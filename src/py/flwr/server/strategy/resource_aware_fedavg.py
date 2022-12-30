#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

from collections import defaultdict
from logging import WARNING
from typing import Any, Callable, Dict, List, Optional, Tuple, Union, cast

import numpy as np
import numpy.typing as npt
import ray
from numpy.polynomial.polynomial import Polynomial
from numpy.random import choice
from ray.experimental.state.api import list_actors
from ray.util.scheduling_strategies import NodeAffinitySchedulingStrategy

from flwr.common import (
    EvaluateRes,
    FitIns,
    FitRes,
    MetricsAggregationFn,
    NDArrays,
    Parameters,
    Scalar,
    ndarrays_to_parameters,
    parameters_to_ndarrays,
)

# from flwr.common.logger import log
# from flwr.common.typing import Config
from flwr.monitoring.profiler import (
    SimpleCPU,
    SimpleCPUProcess,
    SimpleGPU,
    SimpleGPUProcess,
    Task,
)

from flwr.server.client_manager import ClientManager

from flwr.server.client_proxy import ClientProxy
from flwr.server.strategy import FedAvg

# pylint: disable=too-many-locals

MAX_POLY_DEG: int = 15


class ResourceAwareFedAvg(FedAvg):
    """Configurable ResourceAwareFedAvg strategy implementation."""

    # pylint: disable=too-many-arguments,too-many-instance-attributes
    def __init__(
        self,
        fraction_fit: float = 0.1,
        fraction_evaluate: float = 0.1,
        min_fit_clients: int = 2,
        min_evaluate_clients: int = 2,
        min_available_clients: int = 2,
        resource_poly_degree: int = 1,
        evaluate_fn: Optional[
            Callable[
                [int, NDArrays, Dict[str, Scalar]],
                Optional[Tuple[float, Dict[str, Scalar]]],
            ]
        ] = None,
        on_fit_config_fn: Optional[Callable[[int], Dict[str, Scalar]]] = None,
        on_evaluate_config_fn: Optional[Callable[[int], Dict[str, Scalar]]] = None,
        accept_failures: bool = True,
        initial_parameters: Optional[Parameters] = None,
        fit_metrics_aggregation_fn: Optional[MetricsAggregationFn] = None,
        evaluate_metrics_aggregation_fn: Optional[MetricsAggregationFn] = None,
        monitor_namespace: str = "flwr_experiment",
        profiles: Dict[
            str, Dict[str, int]
        ] = {},  # Eventually, change this to List[Profiles]
    ) -> None:
        super().__init__(
            fraction_fit=fraction_fit,
            fraction_evaluate=fraction_evaluate,
            min_fit_clients=min_fit_clients,
            min_evaluate_clients=min_evaluate_clients,
            min_available_clients=min_available_clients,
            evaluate_fn=evaluate_fn,
            on_fit_config_fn=on_fit_config_fn,
            on_evaluate_config_fn=on_evaluate_config_fn,
            accept_failures=accept_failures,
            initial_parameters=initial_parameters,
            fit_metrics_aggregation_fn=fit_metrics_aggregation_fn,
            evaluate_metrics_aggregation_fn=evaluate_metrics_aggregation_fn,
        )
        # will store the updated resources for clients in the round
        self.current_round: int = 0
        self.warm_up_steps = choice(
            list(range(5, 31, 5)), size=resource_poly_degree + 1, replace=False
        )
        self.client_configs_map: Dict[
            str, Tuple[str, str, int]
        ] = {}  # {client_id:(node_id, uuid, num_steps)}
        self.namespace = monitor_namespace
        self.profiles = profiles
        self.resource_poly_degree: int = resource_poly_degree
        self.cpu_resources: Dict[str, SimpleCPU] = {}  # node_id: simple_cpu
        self.gpu_resources: Dict[
            str, Dict[str, SimpleGPU]
        ] = {}  # node_id: {gpu_uuid:  simple_gpu}
        self.resources_model: Dict[
            Tuple[str, str], Tuple[Polynomial, int]
        ] = {}  # {(node_id, gpu_uuid): (Polynomial, max_num_clients)

    def __repr__(self) -> str:
        rep = f"ResourceAwareFedAvg(accept_failures={self.accept_failures})"
        return rep

    def _get_monitors(self):
        actors = list_actors(
            filters=[
                ("state", "=", "ALIVE"),
                ("class_name", "=", "RaySystemMonitor"),
            ]
        )
        return actors

    def _start_data_collection(self):
        actors = self._get_monitors()
        for actor in actors:
            node_id: str = actor["name"]
            this_actor = ray.get_actor(node_id, namespace=self.namespace)
            this_actor.run.remote()

    def _stop_data_collection(self):
        actors = self._get_monitors()
        for actor in actors:
            node_id: str = actor["name"]
            this_actor = ray.get_actor(node_id, namespace=self.namespace)
            ray.get(this_actor.stop.remote())

    def request_available_resources(self) -> None:
        actors = self._get_monitors()
        for actor in actors:
            node_id: str = actor["name"]
            this_actor = ray.get_actor(node_id, namespace=self.namespace)

            # Start System Monitor
            ray.get(this_actor.start.remote())

            # Get resources
            obj_ref = this_actor.get_resources.remote()
            this_node_resources = cast(
                Dict[str, Union[SimpleCPU, SimpleGPU]], ray.get(obj_ref)
            )
            # Create initial model for each GPU
            self.gpu_resources[node_id] = {}
            for k, v in this_node_resources.items():
                if isinstance(v, SimpleGPU):  # Only get GPUs for now
                    self.gpu_resources[node_id][k] = v
                    gpu_uuid = v.uuid
                    self.create_model(node_id=node_id, gpu_uuid=gpu_uuid)
                elif isinstance(v, SimpleCPU):  # Only get GPUs for now
                    self.cpu_resources[node_id] = v

    def create_model(self, node_id: str, gpu_uuid: str):
        coefficients = (self.resource_poly_degree + 1) * [0.0]
        coefficients[0] = 1.0
        m = Polynomial(coefficients)
        self.resources_model[(node_id, gpu_uuid)] = m, 1

    def generate_client_priority(
        self, clients: List[ClientProxy], properties: Dict[str, Union[float, int]]
    ):  # This entire function should be passed to the strategy as a parameters
        weighted_clients = [(x, properties[x.cid]) for x in clients]
        return weighted_clients

    def associate_resources(
        self,
        parameters: Parameters,
        config: Dict[str, Scalar],
        clients_with_weights: List[Tuple[ClientProxy, int]],
    ) -> List[Tuple[ClientProxy, FitIns]]:
        clients_with_weights.sort(key=lambda x: x[1], reverse=True)

        expected_training_times: List[float] = len(self.resources_model) * [0.0]
        node_gpu_mapping = [k for k in self.resources_model.keys()]

        client_fit_list: List[Tuple[ClientProxy, FitIns]] = []
        while clients_with_weights:
            # Choose which GPU to use, based on time
            idx = expected_training_times.index(min(expected_training_times))
            node_id, gpu_uuid = node_gpu_mapping[idx]
            gpu_id = self.gpu_resources[node_id][gpu_uuid].gpu_id

            # Ray resources
            scheduling_strategy = (
                ray.util.scheduling_strategies.NodeAffinitySchedulingStrategy(
                    node_id=node_id,
                    soft=False,
                )
            )
            this_config = dict(
                config,
                **{
                    "gpu_id": gpu_id,
                },
            )

            # Associate multiple clients to a single GPU if possible (maximize VRAM utilization)
            model_poly, max_num_clients = self.resources_model[(node_id, gpu_uuid)]
            actual_num_clients = min(max_num_clients, len(clients_with_weights))

            these_clients = clients_with_weights[:actual_num_clients]

            # consider largest num_steps
            multi_client_per_gpu_expected_time = model_poly(these_clients[0][1])
            expected_training_times[idx] += multi_client_per_gpu_expected_time

            # Now associate resources
            for client, num_steps in these_clients:
                client.resources["scheduling_strategy"] = scheduling_strategy
                client.resources["num_gpu"] = 1 / actual_num_clients
                self.client_configs_map[client.cid] = (node_id, gpu_uuid, num_steps)
                client_fit_list.append((client, FitIns(parameters, this_config)))

            del clients_with_weights[:actual_num_clients]

        print(f"Expected training time: {max(expected_training_times)} seconds.")

        return client_fit_list

    def configure_fit(
        self,
        server_round: int,
        parameters: Parameters,
        client_manager: ClientManager,
    ) -> List[Tuple[ClientProxy, FitIns]]:
        """Configures the next round of training and allocates resources accordingly."""

        config = {}
        if self.on_fit_config_fn is not None:
            # Custom fit config function provided
            config = self.on_fit_config_fn(server_round)

        client_fit_list: List[Tuple[ClientProxy, FitIns]] = []

        if server_round == 1:
            # Calculate MAX NUM_CLIENTS PER GPU
            self.request_available_resources()

            # Sample one client per device
            clients: List[ClientProxy] = client_manager.sample(
                num_clients=len(self.resources_model),
                min_num_clients=len(self.resources_model),
            )

            # Associate one client per GPU
            for node_id, gpu_uuid in self.resources_model.keys():
                gpu_id = self.gpu_resources[node_id][gpu_uuid].gpu_id
                scheduling_strategy = (
                    ray.util.scheduling_strategies.NodeAffinitySchedulingStrategy(
                        node_id=node_id,
                        soft=False,
                    )
                )
                client = clients.pop()
                num_local_steps = 20
                this_config = dict(
                    config,
                    **{
                        "epochs": 1,
                        "gpu_id": gpu_id,
                        "local_steps": num_local_steps,
                    },
                )
                # Ray node allocation
                client.resources["scheduling_strategy"] = scheduling_strategy
                self.client_configs_map[client.cid] = (
                    node_id,
                    gpu_uuid,
                    num_local_steps,
                )
                client_fit_list.append((client, FitIns(parameters, this_config)))

        elif server_round == 2:
            # Now time the usage considering the maximum number of clients
            clients: List[ClientProxy] = client_manager.sample(
                num_clients=(self.resource_poly_degree + 1) * len(self.resources_model),
                min_num_clients=(self.resource_poly_degree + 1)
                * len(self.resources_model),
            )

            for node_id, gpu_uuid in self.resources_model.keys():
                scheduling_strategy = (
                    ray.util.scheduling_strategies.NodeAffinitySchedulingStrategy(
                        node_id=node_id,
                        soft=False,
                    )
                )
                gpu_id = self.gpu_resources[node_id][gpu_uuid].gpu_id
                for num_local_steps in self.warm_up_steps:
                    client = clients.pop()
                    # Ray node allocation
                    client.resources["scheduling_strategy"] = scheduling_strategy
                    this_config = dict(
                        config,
                        **{
                            "epochs": 1,
                            "gpu_id": gpu_id,
                            "local_steps": num_local_steps,
                        },
                    )
                    self.client_configs_map[client.cid] = (
                        node_id,
                        gpu_uuid,
                        num_local_steps,
                    )
                    client_fit_list.append((client, FitIns(parameters, this_config)))

        else:  # All other rounds
            print(f"CONFIG FIT {server_round} !!!!!!!!!")
            # Sample clients
            sample_size, min_num_clients = self.num_fit_clients(
                client_manager.num_available()
            )
            clients = client_manager.sample(
                num_clients=sample_size, min_num_clients=min_num_clients
            )
            clients_with_weights = [
                (client, self.profiles["train"][client.cid]) for client in clients
            ]

            # Return client/config pairs
            client_fit_list = self.associate_resources(
                parameters, config, clients_with_weights
            )

        self._start_data_collection()
        return client_fit_list

    def aggregate_fit(
        self,
        server_round: int,
        results: List[Tuple[ClientProxy, FitRes]],
        failures: List[Union[Tuple[ClientProxy, FitRes], BaseException]],
    ) -> Tuple[Optional[Parameters], Dict[str, Scalar]]:
        """Aggregate fit results using weighted average."""
        self._stop_data_collection()

        # Task_id used in this round
        round_tasks_to_cid: Dict[Scalar, str] = {}
        for client, fit_res in results:
            this_task_id = fit_res.metrics["_flwr.monitoring.task_id"]
            round_tasks_to_cid[this_task_id] = client.cid

        if server_round == 1:  # Calculate maximum number of clients per GPU
            # Create client-task_id mapping

            actors = self._get_monitors()
            for actor in actors:
                node_id: str = actor["name"]
                this_monitor = ray.get_actor(node_id, namespace=self.namespace)
                obj_ref = this_monitor.aggregate_statistics.remote(
                    task_ids=[k for k in round_tasks_to_cid.keys()]
                )
                this_monitor_metrics = ray.get(obj_ref)

                # Get GPU memory usage. Here we consider single gpu per task,
                # But we should also consider all possible combinations of multiple GPUs as well.
                max_this_proc_mem_used_mb: Dict[
                    str, Dict[str, float]  # {task_id:{gpu_uuid:float}}
                ] = this_monitor_metrics["max_this_proc_mem_used_mb"]
                max_all_proc_mem_used_mb = this_monitor_metrics[
                    "max_all_proc_mem_used_mb"
                ]

                for this_task, gpu_mem_dict in max_this_proc_mem_used_mb.items():
                    for (gpu_uuid, max_mem_used_this_gpu) in gpu_mem_dict.items():
                        total_mem_this_gpu = self.gpu_resources[node_id][
                            gpu_uuid
                        ].total_mem_mb
                        max_all_proc_mem = max_all_proc_mem_used_mb[gpu_uuid]

                        max_num_clients_this_gpu = int(
                            (
                                total_mem_this_gpu
                                - max_all_proc_mem
                                + max_mem_used_this_gpu
                            )
                            / max_mem_used_this_gpu
                        )
                        # Update the resource_model max number of clients per gpu
                        poly_model, t = self.resources_model[(node_id, gpu_uuid)]
                        self.resources_model[(node_id, gpu_uuid)] = (
                            poly_model,
                            max_num_clients_this_gpu,
                        )
                        print(
                            f"Maximum number of clients for {node_id}, {gpu_uuid} = {max_num_clients_this_gpu}"
                        )

        elif server_round == 2:
            print(f"AGGREGATE FIT {server_round} !!!!!!!!!")
            task_to_cid: Dict[Scalar, str] = {}
            for client, result in results:
                this_task_id = result.metrics["_flwr.monitoring.task_id"]
                task_to_cid[this_task_id] = client.cid

            actors = self._get_monitors()
            for actor in actors:
                node_id: str = actor["name"]
                this_monitor = ray.get_actor(node_id, namespace=self.namespace)
                obj_ref = this_monitor.aggregate_statistics.remote(
                    task_ids=[k for k in task_to_cid.keys()]
                )
                this_monitor_metrics = ray.get(obj_ref)

                # Calculate time model for maximum usage per GPU
                resource_model_x = defaultdict(list)
                resource_model_y = defaultdict(list)

                for task_id, training_time_ns in this_monitor_metrics[
                    "training_times_ns"
                ].items():
                    cid = task_to_cid[task_id]
                    node_id, gpu_uuid, num_steps = self.client_configs_map[cid]
                    resource_model_x[(node_id, gpu_uuid)].append(num_steps)
                    resource_model_y[(node_id, gpu_uuid)].append(training_time_ns / 1e9)

                # fit Polynomials
                for k, v in self.resources_model.items():
                    node_id, gpu_uuid = k
                    m, max_num_clients = v
                    x = resource_model_x[(node_id, gpu_uuid)]
                    y = resource_model_y[(node_id, gpu_uuid)]
                    m = m.fit(x, y, deg=self.resource_poly_degree)
                    self.resources_model[(node_id, gpu_uuid)] = (m, max_num_clients)

        return super().aggregate_fit(server_round, results, failures)
