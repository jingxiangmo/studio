import time
import asyncio
from queue import Queue
from threading import Thread
import json
import os
import networkx as nx
from PYTHON.task_queue.worker import Worker
from captain.internal.manager import Manager
from captain.models.topology import Topology
from typing import Any, Callable
from captain.types.flowchart import PostWFC
from captain.utils.logger import logger
from subprocess import Popen, PIPE
import importlib.metadata
from .status_codes import STATUS_CODES
from flojoy.utils import clear_flojoy_memory
from captain.types.worker import WorkerJobResponse, ModalConfig
import traceback
from captain.utils.broadcast import (
    broadcast_worker_response,
    signal_max_runtime_exceeded,
    signal_standby,
    signal_prejob_op,
)
from captain.utils.import_nodes import pre_import_functions
import logging


def run_worker(
    task_queue: Queue[Any], imported_functions: dict[str, Any], node_delay: float
):
    try:
        # TODO: Figure out a way to make this work with python threads (previously this was a Python Process)
        # if (
        #     os.environ.get("DEBUG", None) is None
        #     or os.environ.get("DEBUG", None) == "False"
        # ):
        #     text_trap = io.StringIO()
        #     sys.stdout = text_trap
        logger.debug("Starting worker")
        worker = Worker(
            task_queue=task_queue,
            imported_functions=imported_functions,
            node_delay=node_delay,
        )
        worker.run()
    except Exception as e:
        print(f"Error in worker: {e} {traceback.format_exc()}", flush=True)


def create_topology(
    request: PostWFC,
    task_queue: Queue[Any],
    cleanup_func: Callable[..., Any],
    worker_response: Callable[..., Any],
    final_broadcast: Callable[..., Any],
):
    graph = flowchart_to_nx_graph(json.loads(request.fc))
    return Topology(
        graph=graph,
        jobset_id=request.jobsetId,
        task_queue=task_queue,
        cleanup_func=cleanup_func,
        worker_response=worker_response,
        node_delay=request.nodeDelay / 1000,
        final_broadcast=final_broadcast,
    )


# spawns a set amount of workers to execute jobs (node functions)
def spawn_workers(
    manager: Manager,
    imported_functions: dict[str, Any],
    node_delay: float,
    max_workers: int,
):
    if manager.running_topology is None:
        logger.error("Could not spawn workers, no topology detected")
        return
    worker_number = manager.running_topology.get_maximum_workers(
        maximum_capacity=max_workers
    )
    logger.debug(f"NEED {worker_number} WORKERS")
    logger.info(f"Spawning {worker_number} workers")
    manager.thread_count = worker_number
    os.environ["OBJC_DISABLE_INITIALIZE_FORK_SAFETY"] = "YES"
    for _ in range(worker_number):
        worker_process = Thread(
            target=run_worker, args=(manager.task_queue, imported_functions, node_delay)
        )
        worker_process.daemon = True
        worker_process.start()


# converts the dict to a networkx graph
def flowchart_to_nx_graph(flowchart: dict[str, Any]):
    elems = flowchart["nodes"]
    edges = flowchart["edges"]
    nx_graph: nx.MultiDiGraph = nx.MultiDiGraph()
    dict_node_inputs: dict[str, list[Any]] = dict()

    for i in range(len(elems)):
        el = elems[i]
        node_id = el["id"]
        data = el["data"]
        cmd = el["data"]["func"]
        ctrls = data.get("ctrls", {})
        init_ctrls = data.get("initCtrls", {})
        inputs = data.get("inputs", {})
        label = data.get("label", "")
        dict_node_inputs[node_id] = inputs
        node_path = data.get("path", "")
        nx_graph.add_node(
            node_id,
            pos=(el["position"]["x"], el["position"]["y"]),
            id=el["id"],
            ctrls=ctrls,
            init_ctrls=init_ctrls,
            inputs=inputs,
            label=label,
            cmd=cmd,
            node_path=node_path,
        )

    for i in range(len(edges)):
        e = edges[i]
        _id = e["id"]
        u = e["source"]
        v = e["target"]
        label = e["sourceHandle"]
        target_label_id = e["targetHandle"]
        v_inputs = dict_node_inputs[v]
        target_input = next(
            filter(
                lambda input, target_label_id=target_label_id: input.get("id", "")
                == target_label_id,
                v_inputs,
            ),
            None,
        )
        logger.debug(f"----target_input----\n{target_input}")
        target_label = "default"
        multiple = False
        if target_input:
            target_label = target_input.get("name", "default")
            multiple = target_input.get("multiple", False)

        logger.debug(
            f"Adding edge from {u} to {v}\n,"
            f"inputs: {v_inputs}, chosen label: {target_label},\n"
            f"target_label_id: {target_label_id}"
        )
        nx_graph.add_edge(
            u, v, label=label, target_label=target_label, id=_id, multiple=multiple
        )

    return nx_graph


# clears memory used by some worker nodes and job results
def clear_memory():
    clear_flojoy_memory()


async def run_flow_chart(manager: Manager):
    # run the flowchart
    if manager.running_topology:
        asyncio.create_task(manager.running_topology.run())


async def prepare_jobs_and_run_fc(request: PostWFC, manager: Manager):
    pre_job_op_start = time.time()
    logger.debug(f"Pre job operation started at: {pre_job_op_start}")
    socket_msg = WorkerJobResponse(jobset_id=request.jobsetId)
    fc = json.loads(request.fc)

    def clean_up_function(is_finished: bool = False):
        manager.end_worker_threads()
        clear_memory()
        if is_finished:
            asyncio.create_task(signal_standby(manager, request.jobsetId))

    # clean up before next run
    clean_up_function()

    logger.info("BUILDING_TOPOLOGY")
    await manager.ws.broadcast(
        WorkerJobResponse(
            jobset_id=request.jobsetId, sys_status=STATUS_CODES["BUILDING_TOPOLOGY"]
        )
    )

    # Create new task queue
    manager.task_queue = Queue()

    # Create the topology
    manager.running_topology = create_topology(
        request,
        manager.task_queue,
        cleanup_func=clean_up_function,
        worker_response=lambda x: broadcast_worker_response(manager, x),
        final_broadcast=lambda: signal_standby(manager, request.jobsetId),
    )  # pass clean up func for when topology ends

    logger.info("PREJOB_OP")

    pre_job_op_start = time.time()
    logger.debug(f"Pre job operation started at: {pre_job_op_start}")
    await asyncio.create_task(signal_prejob_op(manager, request.jobsetId))

    nodes = fc["nodes"]
    packages_dict = {
        package.name: package.version for package in importlib.metadata.distributions()
    }
    missing_packages = []
    socket_msg["SYSTEM_STATUS"] = STATUS_CODES["COLLECTING_PIP_DEPENDENCIES"]
    await asyncio.create_task(manager.ws.broadcast(socket_msg))
    for node in nodes:
        node_logger = logging.getLogger(node["data"]["func"])
        handler = BroadcastNodeLogs(
            manager=manager, jobset_id=request.jobsetId, node_func=node["data"]["func"]
        )
        handler.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(message)s"))
        node_logger.addHandler(handler)
        if "pip_dependencies" not in node["data"]:
            continue
        for package in node["data"]["pip_dependencies"]:
            pckg = packages_dict.get(package["name"])
            if not pckg:
                pckg_str = (
                    f"{package['name']}=={package['v']}"
                    if "v" in package
                    else f"{package['name']}"
                )
                logger.debug(f"Package: {package['name']} is missing!")
                missing_packages.append(pckg_str)
            else:
                logger.debug(f"Package: {package['name']} is already installed!")

    if missing_packages:
        socket_msg["SYSTEM_STATUS"] = STATUS_CODES["INSTALLING_PACKAGES"]
        await asyncio.create_task(manager.ws.broadcast(socket_msg))
        socket_msg["MODAL_CONFIG"] = ModalConfig(
            showModal=True,
            description="Installing required dependencies before running the flow chart...",
            messages=f"{', '.join(missing_packages)} packages will be installed with pip!",
        )
        await asyncio.create_task(manager.ws.broadcast(socket_msg))
        installation_succeed = await install_packages(
            missing_packages, socket_msg, manager=manager
        )
        logger.debug(f"installing packages was successful? {installation_succeed}")

        if not installation_succeed:
            socket_msg.MODAL_CONFIG[
                "messages"
            ] = "Pre job operation failed! Look at the errors printed above!"
            socket_msg["SYSTEM_STATUS"] = STATUS_CODES["PRE_JOB_OP_FAILED"]
            await asyncio.create_task(manager.ws.broadcast(socket_msg))
            return
        socket_msg.MODAL_CONFIG["messages"] = "Pre job operation successful!"
        socket_msg.MODAL_CONFIG["showModal"] = False
        await asyncio.create_task(manager.ws.broadcast(socket_msg))

    socket_msg["SYSTEM_STATUS"] = STATUS_CODES["IMPORTING_NODE_FUNCTIONS"]
    socket_msg.MODAL_CONFIG["showModal"] = False
    await asyncio.create_task(manager.ws.broadcast(socket_msg))

    # get the amount of workers needed
    funcs, errs = pre_import_functions(topology=manager.running_topology)

    if errs:
        socket_msg["SYSTEM_STATUS"] = STATUS_CODES["IMPORTING_NODE_FUNCTIONS_FAILED"]
        socket_msg["MODAL_CONFIG"] = ModalConfig(
            showModal=True, messages=f"Preflight check failed! \n {', '.join(errs)}"
        )
        socket_msg.FAILED_NODES = errs
        await asyncio.create_task(manager.ws.broadcast(socket_msg))
        return

    socket_msg["SYSTEM_STATUS"] = STATUS_CODES["RUN_IN_PROCESS"]
    await asyncio.create_task(manager.ws.broadcast(socket_msg))

    spawn_workers(manager, funcs, request.nodeDelay, request.maximumConcurrentWorkers)
    logger.debug(
        f"PRE JOB OPERATION TOOK {time.time() - pre_job_op_start} SECONDS TO COMPLETE"
    )
    asyncio.create_task(run_flow_chart(manager=manager))
    asyncio.create_task(cancel_when_max_time(manager, request))


async def cancel_when_max_time(manager: Manager, request: PostWFC):
    await asyncio.sleep(request.maximumRuntime)
    if manager.running_topology and not manager.running_topology.is_cancelled():
        logger.debug("Maximum runtime exceeded, cancelling topology")
        manager.running_topology.cancel()
        await signal_max_runtime_exceeded(manager, request.jobsetId)


def stream_response(proc: Popen[bytes]):
    while True:
        line = proc.stdout.readline() or proc.stderr.readline()  # type:ignore
        if not line:
            break
        yield line


async def install_packages(
    missing_packages: list[str], socket_msg: WorkerJobResponse, manager: Manager
):
    try:
        cmd = ["pip", "install"] + missing_packages
        proc = Popen(cmd, stdout=PIPE, stderr=PIPE)
        while proc.poll() is None:
            stream = stream_response(proc)
            for line in stream:
                socket_msg.MODAL_CONFIG["messages"] = line.decode(encoding="utf-8")
                await asyncio.create_task(manager.ws.broadcast(socket_msg))
        return_code = proc.returncode
        if return_code != 0:
            return False
        return True
    except Exception as e:
        output = "\n".join(e.args)
        socket_msg.MODAL_CONFIG["messages"] = output
        await asyncio.create_task(manager.ws.broadcast(socket_msg))
        return False


class BroadcastNodeLogs(logging.Handler):
    PCKG_INSTALLATION_COMPLETE = "Pip install complete. Spawning process for function"

    def __init__(self, manager: Manager, jobset_id: str, node_func: str):
        super().__init__()
        self.manager = manager
        self.jobset_id = jobset_id
        self.node_func = node_func

    def emit(self, record):
        log_entry = self.format(record)
        socket_msg = WorkerJobResponse(jobset_id=self.jobset_id)
        socket_msg["SYSTEM_STATUS"] = (
            STATUS_CODES["RUNNING_PYTHON_JOB"] + self.node_func
        )
        socket_msg["MODAL_CONFIG"] = ModalConfig(
            showModal=True, messages=log_entry, title=f"{self.node_func} logs"
        )

        if self.PCKG_INSTALLATION_COMPLETE in log_entry:
            socket_msg["MODAL_CONFIG"]["showModal"] = False
        asyncio.run(self.manager.ws.broadcast(socket_msg))
