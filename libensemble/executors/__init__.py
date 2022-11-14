import importlib
from libensemble.executors.executor import Executor
from libensemble.executors.mpi_executor import MPIExecutor

if importlib.util.find_spec("balsam"):

    try:
        from libensemble.executors.balsam_executor import BalsamExecutor
        __all__ = ["Executor", "MPIExecutor", "BalsamExecutor"]
    except (ModuleNotFoundError, ImportError):
        __all__ = ["Executor", "MPIExecutor"]
