import typing
from typing import Callable, List, Optional, Tuple, Union
import warnings
import numpy as np

import torch
from captum._utils.common import (
    _expand_additional_forward_args,
    _expand_target,
    _format_additional_forward_args,
    _format_output,
    _is_tuple,
    _run_forward,
)
from captum.attr._utils.common import (
    _format_input_baseline,
    _reshape_and_sum,
    _validate_input,
)

from geodesic.utils.svi_batching import _batch_attribution

from captum.attr._utils.approximation_methods import approximation_parameters


from captum._utils.typing import BaselineType, TargetType, TensorOrTupleOfTensorsGeneric
from captum.attr._utils.attribution import GradientAttribution
from captum.log import log_usage
from torch import Tensor

import pyro
import pyro.distributions as dist
from pyro.infer import SVI, Trace_ELBO
from pyro.optim import Adam


class GeodesicIGSVI(GradientAttribution):
    def __init__(
        self,
        forward_func: Callable[..., Tensor],
        multiply_by_inputs: bool = True,
        seed: int = 42,
    ) -> None:
        GradientAttribution.__init__(self, forward_func)
        self._multiply_by_inputs = multiply_by_inputs
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        # Set seeds
        torch.manual_seed(seed)
        if torch.cuda.is_available():
            torch.cuda.manual_seed_all(seed)
        np.random.seed(seed)
        pyro.set_rng_seed(seed)

        # Enable deterministic behavior
        torch.backends.cudnn.deterministic = True
        torch.backends.cudnn.benchmark = False

        self.forward_func = self.forward_func.to(self.device)
        for param in self.forward_func.parameters():
            param.data = param.data.to(self.device)

        print(f"Initialised GeodesicIGSVI on device: {self.device}")

    def _ensure_device(self, tensor_or_tuple):
        """Move tensor or tuple of tensors to correct device."""
        if isinstance(tensor_or_tuple, tuple):
            return tuple(
                t.to(self.device) if t is not None else None for t in tensor_or_tuple
            )
        return tensor_or_tuple.to(self.device) if tensor_or_tuple is not None else None

    def potential_energy(
        self,
        path: Tuple[Tensor, ...],
        initial_paths: Tuple[Tensor, ...],
        beta: float,
        input_additional_args: Tuple[Tensor, ...],
        use_endpoints_matching: bool = True,
    ) -> Tensor:
        """Computes the total potential energy of the path.

        Args:
            path [Tuple[Tensor, ...]]: Tuple of path points. This is the path that we want to optimise.
            initial_paths [Tuple[Tensor, ...]]: Tuple of initial path points. This is the initial straight line path.
            beta [float]: Weight of curvature penalty
            input_additional_args [Tuple[Tensor, ...]]: Additional arguments for forward function
            use_endpoints_matching [bool]: Whether to use endpoint matching penalties. This penalises deviation from the initial path at the start and end.
        Returns:
            Total potential energy (scalar)
        """
        # distance penalty
        distance_penalties = tuple(
            torch.norm(path[i] - initial_paths[i], p=2, dim=-1)
            for i in range(len(path))
        )

        # curvature penalty
        with torch.autograd.set_grad_enabled(True):
            outputs = _run_forward(
                self.forward_func, path, additional_forward_args=input_additional_args
            )
            path_grads = torch.autograd.grad(
                outputs,
                path,
                grad_outputs=torch.ones_like(outputs),
                create_graph=True,
                retain_graph=True,
            )
        curvature_penalties = tuple(
            torch.norm(path_grads[i], p=2, dim=-1) for i in range(len(path_grads))
        )

        total_penalty = sum(
            (distance_penalties[i] - beta * curvature_penalties[i]).sum()
            for i in range(len(distance_penalties))
        )
        if use_endpoints_matching:
            endpoint_weight = 100
            endpoint_penalties = 0
            n_batch = int(path[0].shape[0] // self.n_steps)
            n_features = path[0].shape[1:]
            view_shape = (self.n_steps, n_batch) + n_features

            # Calculate 10% of steps
            n_edge_steps = max(1, int(0.1 * self.n_steps))

            for i in range(len(path)):
                path_reshaped = path[i].view(view_shape)
                initial_reshaped = initial_paths[i].view(view_shape)

                # Penalise first 10% deviation
                endpoint_penalties += (
                    endpoint_weight
                    * torch.norm(
                        path_reshaped[:n_edge_steps] - initial_reshaped[:n_edge_steps],
                        p=2,
                        dim=-1,
                    ).sum()
                )

                # Penalise last 10% deviation
                endpoint_penalties += (
                    endpoint_weight
                    * torch.norm(
                        path_reshaped[-n_edge_steps:]
                        - initial_reshaped[-n_edge_steps:],
                        p=2,
                        dim=-1,
                    ).sum()
                )
            return total_penalty + endpoint_penalties
        else:
            return total_penalty

    def model(
        self,
        initial_paths: Tuple[Tensor, ...],
        beta: float,
        input_additional_args: Tuple[Tensor, ...],
        use_endpoints_matching: bool = True,
    ) -> None:
        """
        Defines the model for Stochastic Variational Inference (SVI) optimization.

        This model samples perturbations to the initial paths and computes the
        potential energy of the resulting paths. The energy is then used to
        define a factor in the probabilistic model.

        Args:
            initial_paths (Tuple[Tensor, ...]): Tuple of initial path points.
                These points define the starting paths for optimization.
            beta (float): Weight of the curvature penalty. Higher values
                penalise curvature more strongly.
            input_additional_args (Tuple[Tensor, ...]): Additional arguments
                required for the forward function.
            use_endpoints_matching (bool, optional): If True, applies penalties
                to ensure the endpoints of the paths match the initial paths.
                Defaults to True.
        """
        initial_paths = self._ensure_device(initial_paths)
        input_additional_args = self._ensure_device(input_additional_args)

        delta_tuple = tuple(
            pyro.sample(
                f"path_delta_{i}",
                dist.Normal(
                    torch.zeros_like(initial_paths[i]).to(self.device),
                    torch.ones_like(initial_paths[i]).to(self.device),
                ).to_event(initial_paths[i].dim()),
            )
            for i in range(len(initial_paths))
        )

        paths = tuple(
            (initial_paths[i] + delta_tuple[i]).requires_grad_()
            for i in range(len(initial_paths))
        )

        energy = self.potential_energy(
            paths,
            initial_paths,
            beta,
            input_additional_args,
            use_endpoints_matching=use_endpoints_matching,
        )
        pyro.factor("energy", -energy)

    def guide(
        self,
        initial_paths: Tuple[Tensor, ...],
        beta: float,
        input_additional_args: Tuple[Tensor, ...],
        use_endpoints_matching: bool = True,
    ) -> Tuple[Tensor, ...]:
        """
        Guide function for Stochastic Variational Inference (SVI) optimisation.

        This guide function learns the optimal deviations from the initial paths
        by parameterizing the perturbations. It uses variational parameters to
        sample the deviations and returns the optimised paths.

        Args:
            initial_paths (Tuple[Tensor, ...]): Tuple of initial path points.
                These points define the starting paths for optimisation.
            beta (float): Weight of the curvature penalty. Higher values
                penalise curvature more strongly.
            input_additional_args (Tuple[Tensor, ...]): Additional arguments
                required for the forward function.
            use_endpoints_matching (bool, optional): If True, applies penalties
                to ensure the endpoints of the paths match the initial paths.
                Defaults to True.

        Returns:
            Tuple[Tensor, ...]: Tuple of optimised paths.
        """
        initial_paths = self._ensure_device(initial_paths)

        delta_locs = tuple(
            pyro.param(f"delta_loc_{i}", lambda: torch.zeros_like(initial_paths[i])).to(
                self.device
            )
            for i in range(len(initial_paths))
        )

        delta_scales = tuple(
            pyro.param(
                f"delta_scale_{i}",
                lambda: 0.1 * torch.ones_like(initial_paths[i]),
                constraint=dist.constraints.positive,
            ).to(self.device)
            for i in range(len(initial_paths))
        )

        for i in range(len(initial_paths)):
            pyro.sample(
                f"path_delta_{i}",
                dist.Normal(delta_locs[i], delta_scales[i]).to_event(
                    initial_paths[i].dim()
                ),
            )

        optimised_paths = tuple(
            (initial_paths[i] + delta_locs[i]).requires_grad_()
            for i in range(len(initial_paths))
        )
        return optimised_paths

    def _optimise_paths(
        self,
        initial_paths: Tuple[Tensor, ...],
        input_additional_args: Tuple[Tensor, ...],
        beta_decay_rate: float,
        current_beta: float = 0.3,
        num_iterations: int = 100000,
        initial_lr: float = 1e-3,
        min_lr: float = 1e-5,
        lr_decay_factor: float = 0.5,
        lr_patience: int = 25,
        use_endpoints_matching: bool = True,
        do_linear_interp: bool = True,
        patience: int = 4000,
        rel_improvement_threshold: float = 1e-4,
    ) -> Tensor:
        """
        Optimises the paths using Stochastic Variational Inference (SVI).

        This function performs SVI to optimise the paths by minimizing the loss
        function. It adjusts the learning rate dynamically and applies early
        stopping based on the relative improvement of the loss.

        Args:
            initial_paths (Tuple[Tensor, ...]): Tuple of initial path points.
            input_additional_args (Tuple[Tensor, ...]): Additional arguments
                required for the forward function.
            beta_decay_rate (float): Decay rate for the beta parameter.
            current_beta (float, optional): Initial value of the beta parameter.
                Defaults to 0.3.
            num_iterations (int, optional): Number of iterations for optimization.
                Defaults to 100000.
            initial_lr (float, optional): Initial learning rate. Defaults to 1e-3.
            min_lr (float, optional): Minimum learning rate. Defaults to 1e-5.
            lr_decay_factor (float, optional): Factor by which to decay the learning
                rate. Defaults to 0.5.
            lr_patience (int, optional): Number of iterations to wait before
                decaying the learning rate if no improvement. Defaults to 25.
            use_endpoints_matching (bool, optional): If True, applies penalties to
                ensure the endpoints of the paths match the initial paths. Defaults
                to True.
            do_linear_interp (bool, optional): If True, performs linear interpolation
                on the optimised paths. Defaults to True.
            patience (int, optional): Number of iterations to wait before early
                stopping if no improvement. Defaults to 4000.
            rel_improvement_threshold (float, optional): Relative improvement
                threshold for early stopping. Defaults to 1e-4.

        Returns:
            Tensor: Optimised paths.
        """
        with torch.no_grad():
            initial_paths = self._ensure_device(initial_paths)
            input_additional_args = self._ensure_device(input_additional_args)

            current_lr = initial_lr
            optimiser = Adam({"lr": current_lr})
            svi = SVI(
                model=self.model,
                guide=self.guide,
                optim=optimiser,
                loss=Trace_ELBO(retain_graph=True),
            )

            best_loss = float("inf")
            patience_counter = 0
            lr_patience_counter = 0
            loss_history = []
            beta = current_beta

            for step in range(num_iterations):
                loss = svi.step(
                    initial_paths,
                    beta,
                    input_additional_args,
                    use_endpoints_matching=use_endpoints_matching,
                )
                loss_history.append(loss)
                beta *= beta_decay_rate

                if len(loss_history) > 1:
                    rel_improvement = (loss_history[-2] - loss) / loss_history[-2]

                    if loss < best_loss:
                        best_loss = loss
                        patience_counter = 0
                        lr_patience_counter = 0
                    else:
                        patience_counter += 1
                        lr_patience_counter += 1

                        # Decay learning rate if no improvement
                        if lr_patience_counter >= lr_patience and current_lr > min_lr:
                            current_lr = max(current_lr * lr_decay_factor, min_lr)
                            # Create new optimiser with updated learning rate
                            optimiser = Adam({"lr": current_lr})
                            svi.optim = optimiser
                            lr_patience_counter = 0
                            print(f"Decreasing learning rate to {current_lr:.6f}")

                    if (
                        rel_improvement < rel_improvement_threshold
                        and patience_counter >= patience
                    ):
                        print(
                            f"Early stopping at step {step}: Loss converged with relative improvement {rel_improvement:.6f}"
                        )
                        break
                if step % 100 == 0:
                    print(
                        f"Step {step}: loss = {loss:.3f}, beta = {beta:.3f}, lr = {current_lr:.6f}"
                    )

            with torch.no_grad():
                optimised_paths = self.guide(
                    initial_paths,
                    beta,
                    input_additional_args,
                    use_endpoints_matching=use_endpoints_matching,
                )
                optimised_paths = self._ensure_device(optimised_paths)

            if do_linear_interp:
                print("Interpolating paths...")
                optimised_paths = tuple(
                    self.make_uniform_spacing(opt_paths, n_steps=self.n_steps)
                    for opt_paths in optimised_paths
                )

            return optimised_paths

    def make_uniform_spacing(self, paths: Tensor, n_steps: int) -> Tuple[Tensor, int]:
        device = paths.device
        batch_size = paths.shape[0] // n_steps
        feature_dims = paths.shape[1:]

        step_sizes = calculate_step_sizes(
            paths, n_inputs=batch_size, n_features=feature_dims, n_steps=n_steps
        )
        standardized_step_sizes = step_sizes / step_sizes.sum(dim=0).unsqueeze(0)

        paths = paths.view(n_steps, batch_size, *feature_dims)
        standardised_step_sizes = standardised_step_sizes.view(n_steps, batch_size, 1)

        dense_paths = [[] for _ in range(batch_size)]

        for j in range(batch_size):
            starts = paths[:-1, j]
            ends = paths[1:, j]

            max_step = standardised_step_sizes.max().item()
            scale_factor = n_steps / max_step
            num_points = (standardised_step_sizes[:, j] * scale_factor).long()

            all_points = []
            all_points.append(paths[0, j].unsqueeze(0))

            for i in range(n_steps - 1):
                n = num_points[i].item()
                alphas = torch.linspace(0, 1, n + 1, device=device).view(
                    -1, *([1] * len(feature_dims))
                )
                segment_points = starts[i : i + 1] + alphas * (
                    ends[i : i + 1] - starts[i : i + 1]
                )
                all_points.append(segment_points)

            dense_path = torch.cat(all_points, dim=0)
            indices = torch.linspace(
                0, len(dense_path) - 1, n_steps, device=device
            ).long()
            dense_paths[j] = dense_path[indices]

        return (
            torch.stack(dense_paths)
            .transpose(0, 1)
            .reshape(n_steps * batch_size, *feature_dims)
        )

    @log_usage()
    def attribute(
        self,
        inputs: TensorOrTupleOfTensorsGeneric,
        baselines: BaselineType = None,
        augmentation_data: Tensor = None,
        target: TargetType = None,
        additional_forward_args: Optional[object] = None,
        n_steps: int = 50,
        method: str = "gausslegendre",
        internal_batch_size: Union[None, int] = None,
        return_convergence_delta: bool = False,
        return_paths: bool = False,
        beta: float = 0.3,
        n_neighbors: int = 20,
        num_iterations: int = 1000,
        learning_rate: float = 0.001,
        use_endpoints_matching: bool = True,
        do_linear_interp: bool = True,
    ) -> Union[
        TensorOrTupleOfTensorsGeneric, Tuple[TensorOrTupleOfTensorsGeneric, Tensor]
    ]:
        """This is similar to IntegratedGradients, but instead of integrating
        over a straight line, we use the SVI method to integrate over a
        geodesic path.

        Geodesic paths are shortest paths between two points on a
        manifold. They avoid regions of high curvature, which are
        regions of high log-likelihood gradient.
        """
        if augmentation_data is not None and n_neighbors is None:
            raise ValueError(
                "Augmentation data is provided, but no n_neighbors is given. Please provide a n_neighbors."
            )
        if augmentation_data is None and n_neighbors is not None:
            warnings.warn(
                "n_neighbors is provided, but no augmentation data is given. Ignoring n_neighbors."
            )
        self.internal_batch_size = internal_batch_size
        self.n_steps = n_steps
        # Keeps track whether original input is a tuple or not before
        # converting it into a tuple.
        is_inputs_tuple = _is_tuple(inputs)

        formatted_inputs, formatted_baselines = _format_input_baseline(
            inputs, baselines
        )

        _validate_input(formatted_inputs, formatted_baselines, n_steps, method)
        paths = None
        if internal_batch_size is not None:
            num_examples = formatted_inputs[0].shape[0]
            attributions = _batch_attribution(
                self,
                num_examples,
                internal_batch_size,
                n_steps,
                inputs=formatted_inputs,
                baselines=formatted_baselines,
                augmentation_data=augmentation_data,
                target=target,
                additional_forward_args=additional_forward_args,
                method=method,
                beta=beta,
                num_iterations=num_iterations,
                learning_rate=learning_rate,
            )
        else:
            attributions, paths = self._attribute(
                inputs=formatted_inputs,
                baselines=formatted_baselines,
                augmentation_data=augmentation_data,
                target=target,
                additional_forward_args=additional_forward_args,
                n_steps=n_steps,
                n_neighbors=n_neighbors,
                method=method,
                beta=beta,
                num_iterations=num_iterations,
                learning_rate=learning_rate,
                use_endpoints_matching=use_endpoints_matching,
                do_linear_interp=do_linear_interp,
            )
        formatted_outputs = _format_output(is_inputs_tuple, attributions)
        if return_convergence_delta:
            start_point, end_point = baselines, inputs
            # computes approximation error based on the completeness axiom
            delta = self.compute_convergence_delta(
                attributions,
                start_point,
                end_point,
                additional_forward_args=additional_forward_args,
                target=target,
            )

        returned_variables = []
        returned_variables.append(formatted_outputs)
        if return_paths:
            returned_variables.append(paths)
        if return_convergence_delta:
            returned_variables.append(delta)
        return (
            tuple(returned_variables)
            if len(returned_variables) > 1
            else formatted_outputs
        )

    def _attribute(
        self,
        inputs: Tuple[Tensor, ...],
        baselines: Tuple[Union[Tensor, int, float], ...],
        augmentation_data: Tensor = None,
        target: TargetType = None,
        additional_forward_args: Optional[object] = None,
        n_steps: int = 50,
        n_neighbors: int = 20,
        method: Union[None, str] = None,
        step_sizes_and_alphas: Union[None, Tuple[List[float], List[float]]] = None,
        num_iterations: int = 1000,
        beta: float = 0.3,
        learning_rate: float = 0.01,
        use_endpoints_matching: bool = True,
        do_linear_interp: bool = True,
    ) -> Tuple[Tensor, ...]:

        if step_sizes_and_alphas is None:
            step_sizes_func, alphas_func = approximation_parameters(method)
            step_sizes, alphas = step_sizes_func(n_steps), alphas_func(n_steps)
        else:
            step_sizes, alphas = step_sizes_and_alphas

        straight_line_tuple = tuple(
            torch.cat(
                [baseline + alpha * (input - baseline) for alpha in alphas], dim=0
            ).requires_grad_()
            for input, baseline in zip(inputs, baselines)
        )  # straight line between input and baseline. Dim of each tensor in tuple: [n_steps * batch_size, n_features]

        if augmentation_data is not None:
            initial_paths = self._get_approx_paths(
                inputs,
                baselines,
                augmentation_data,
                alphas,
                self.n_steps,
                n_neighbors,
            )

            beta = 1 / beta if beta > 1 else beta
            current_beta = beta * 10
            beta_decay_rate = (current_beta * beta) ** (1 / num_iterations)
        else:
            initial_paths = straight_line_tuple
            current_beta = beta
            beta_decay_rate = 1

        additional_forward_args = _format_additional_forward_args(
            additional_forward_args
        )
        # apply number of steps to additional forward args
        # currently, number of steps is applied only to additional forward arguments
        # that are nd-tensors. It is assumed that the first dimension is
        # the number of batches.
        # dim -> (bsz * #steps x additional_forward_args[0].shape[1:], ...)
        input_additional_args = (
            _expand_additional_forward_args(additional_forward_args, n_steps)
            if additional_forward_args is not None
            else None
        )

        optimised_paths = self._optimise_paths(
            initial_paths,
            input_additional_args,
            beta_decay_rate,
            current_beta,
            num_iterations,
            initial_lr=learning_rate,
            use_endpoints_matching=use_endpoints_matching,
            do_linear_interp=do_linear_interp,
        )

        n_inputs = tuple(input.shape[0] for input in inputs)
        n_features = tuple(input.shape[1:] for input in inputs)

        step_sizes_tuple = tuple(
            calculate_step_sizes(path, n_inputs[i], self.n_steps, n_features[i])
            for i, path in enumerate(optimised_paths)
        )

        expanded_target = _expand_target(target, self.n_steps)

        # grads: dim -> (bsz * #steps x inputs[0].shape[1:], ...)
        grads = self.gradient_func(
            forward_fn=self.forward_func,
            inputs=optimised_paths,
            target_ind=expanded_target,
            additional_forward_args=input_additional_args,
        )

        # flattening grads so that we can multilpy it with step-size
        # calling contiguous to avoid `memory whole` problems
        scaled_grads = [
            grad.contiguous()
            * step_sizes.view(step_sizes.shape[0], *([1] * (grad.dim() - 1))).to(
                grad.device
            )
            for step_sizes, grad in zip(step_sizes_tuple, grads)
        ]

        # aggregates across all steps for each tensor in the input tuple
        # total_grads has the same dimensionality as inputs
        total_grads = tuple(
            _reshape_and_sum(
                scaled_grad, n_steps, grad.shape[0] // n_steps, grad.shape[1:]
            )
            for (scaled_grad, grad) in zip(scaled_grads, grads)
        )

        # computes attribution for each tensor in input tuple
        # attributions has the same dimensionality as inputs
        if not self.multiplies_by_inputs:
            attributions = total_grads
        else:
            attributions = tuple(
                total_grad * (input - baseline)
                for total_grad, input, baseline in zip(total_grads, inputs, baselines)
            )
        return attributions, optimised_paths


def calculate_step_sizes(path, n_inputs, n_steps, n_features):
    view_shape = (n_steps, n_inputs) + n_features
    paths_reshaped = path.view(view_shape)

    # Calculate initial step sizes
    step_sizes = torch.norm(
        paths_reshaped[1:] - paths_reshaped[:-1],
        p=2,
        dim=tuple(range(2, 2 + len(n_features))),
    )

    # Add final step to match dimensions
    last_step = step_sizes[-1:]
    step_sizes = torch.cat([step_sizes, last_step], dim=0)

    # Reshape to match original path dimensions
    step_sizes = step_sizes.view(n_steps * n_inputs, 1)

    return step_sizes
