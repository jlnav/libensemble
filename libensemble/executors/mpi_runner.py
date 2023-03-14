import argparse
import logging
from libensemble.resources.platforms import GPU_SET_DEF, GPU_SET_ENV, GPU_SET_CLI, GPU_SET_CLI_GPT
#from libensemble.executors.executor import Task, jassert
from libensemble.executors.executor import jassert
from libensemble.resources import mpi_resources
from libensemble.resources.resources import Resources
#from typing import Dict, Optional, Union

logger = logging.getLogger(__name__)
# To change logging level for just this module
# logger.setLevel(logging.DEBUG)


class MPIRunner:
    @staticmethod
    def get_runner(mpi_runner_type, runner_name=None, platform_info=None):
        mpi_runners = {
            "mpich": MPICH_MPIRunner,
            "openmpi": OPENMPI_MPIRunner,
            "aprun": APRUN_MPIRunner,
            "srun": SRUN_MPIRunner,
            "jsrun": JSRUN_MPIRunner,
            "msmpi": MSMPI_MPIRunner,
            "custom": MPIRunner,
        }
        mpi_runner = mpi_runners[mpi_runner_type]
        if runner_name is not None:
            runner = mpi_runner(run_command=runner_name, platform_info=platform_info)
        else:
            runner = mpi_runner(platform_info=platform_info)
        return runner

    def __init__(self, run_command="mpiexec", platform_info=None):
        self.run_command = run_command
        self.mpi_command = [self.run_command, "{extra_args}"]
        self.subgroup_launch = False
        self.mfile_support = False
        self.arg_nprocs = ("--LIBE_NPROCS_ARG_EMPTY",)
        self.arg_nnodes = ("--LIBE_NNODES_ARG_EMPTY",)
        self.arg_ppn = ("--LIBE_PPN_ARG_EMPTY",)
        self.default_mpi_options = None
        self.default_gpu_arg = None
        self.default_gpu_arg_type = None
        self.platform_info = platform_info

    def _get_parser(self, p_args, nprocs, nnodes, ppn):
        parser = argparse.ArgumentParser(description="Parse extra_args", allow_abbrev=False)
        parser.add_argument(*nprocs, type=int, dest="num_procs", default=None)
        parser.add_argument(*nnodes, type=int, dest="num_nodes", default=None)
        parser.add_argument(*ppn, type=int, dest="procs_per_node", default=None)
        args, _ = parser.parse_known_args(p_args)
        return args

    def _parse_extra_args(self, nprocs, nnodes, ppn, hyperthreads, extra_args):
        splt_extra_args = extra_args.split()
        p_args = self._get_parser(splt_extra_args, self.arg_nprocs, self.arg_nnodes, self.arg_ppn)

        # Only fill from extra_args if not set by portable options
        if nprocs is None:
            nprocs = p_args.num_procs
        if nnodes is None:
            nnodes = p_args.num_nodes
        if ppn is None:
            ppn = p_args.procs_per_node

        extra_args = " ".join(splt_extra_args)
        return nprocs, nnodes, ppn, p_args

    def _rm_replicated_args(self, nprocs, nnodes, ppn, p_args):
        if p_args is not None:
            if p_args.num_procs is not None:
                nprocs = None
            if p_args.num_nodes is not None:
                nnodes = None
            if p_args.procs_per_node is not None:
                ppn = None
        return nprocs, nnodes, ppn

    def express_spec(
        self, task, nprocs, nnodes, ppn, machinefile, hyperthreads, extra_args, resources, workerID
    ):
        hostlist = None
        machinefile = None
        # Always use host lists (unless uneven mapping)
        hostlist = mpi_resources.get_hostlist(resources, nnodes)
        return hostlist, machinefile

    def _set_gpu_cli_option(self, wresources, extra_args, gpu_setting_name, gpu_value):
        """Update extra args with the GPU setting for the MPI runner"""
        jassert(wresources.even_slots, f"Cannot assign CPUs/GPUs to uneven slots per node {wresources.slots}")

        if gpu_setting_name.endswith("="):
            gpus_opt = gpu_setting_name + str(gpu_value)
        else:
            gpus_opt = gpu_setting_name + " " + str(gpu_value)

        if extra_args is None:
            extra_args = gpus_opt
        else:
            extra_args = " ".join((extra_args, gpus_opt))
        # print(f"platform read: extra_args: {extra_args}") #Testing
        return extra_args

    def _set_gpu_env_var(self, wresources, task, gpus_env):
        """Add GPU environment variable setting to tasks environment"""
        jassert(wresources.matching_slots, f"Cannot assign CPUs/GPUs to non-matching slots per node {wresources.slots}")
        task._add_to_env(gpus_env, wresources.get_slots_as_string(multiplier=wresources.gpus_per_rset)) # to use avail GPUS.

    #TODO may be unnecesary function - could merge into _assign_to_slots flow with current options
    def _local_runner_set_gpus(self, task, wresources, extra_args, gpus_per_node, nprocs):
        if self.default_gpu_arg is not None:
            arg_type = self.default_gpu_arg_type
            gpu_value = gpus_per_node // nprocs if arg_type == GPU_SET_CLI_GPT else gpus_per_node
            gpu_setting_name = self.default_gpu_arg
            extra_args = self._set_gpu_cli_option(wresources, extra_args, gpu_setting_name, gpu_value)
        else:
            #could be self.default_gpu_arg if allow default_gpu_arg_type to be env but why set by mpi runner
            gpus_env = "CUDA_VISIBLE_DEVICES"
            self._set_gpu_env_var(wresources, task, gpus_env)
        return extra_args

    #TODO: Need to check if nprocs is not set - use task_partition to see if can get a value
    #      need for _local_runner_set_gpus and below in GPU_SET_CLI_GPT clause.
    #      Do this after conversion to nprocs, nnodes, ppn to dict.
    def _assign_to_slots(self, task, resources, nprocs, nnodes, ppn, extra_args, match_procs_to_gpus):
        """Assign GPU resources to slots

        First tries getting method from user settings, otherwise use detection or default.
        """

        wresources = resources.worker_resources
        # gpus_per_node = wresources.slot_count * wresources.gpus_per_rset  # rounds at one rset
        gpus_per_node = wresources.slot_count * wresources.gpus_per_node // wresources.rsets_per_node

        gpu_setting_type = GPU_SET_DEF

        if match_procs_to_gpus:
            nnodes = wresources.local_node_count
            ppn = gpus_per_node
            nprocs = nnodes * ppn
            jassert(nprocs > 0, f"Matching procs to GPUs has resulted in {nprocs} procs")
            # print(f"num nodes {nnodes} procs_per_node {ppn}") #Testing

        if self.platform_info is not None:
            gpu_setting_type = self.platform_info.get("gpu_setting_type", gpu_setting_type)

        if gpu_setting_type == GPU_SET_DEF:
            extra_args = self._local_runner_set_gpus(task, wresources, extra_args, gpus_per_node, nprocs)

        elif gpu_setting_type in [GPU_SET_CLI, GPU_SET_CLI_GPT]:
            gpu_value = gpus_per_node // nprocs if gpu_setting_type == GPU_SET_CLI_GPT else gpus_per_node
            gpu_setting_name = self.platform_info.get("gpu_setting_name", self.default_gpu_arg)
            extra_args = self._set_gpu_cli_option(wresources, extra_args, gpu_setting_name, gpu_value)

        elif gpu_setting_type == GPU_SET_ENV:
            gpus_env = self.platform_info.get("gpu_setting_name", "CUDA_VISIBLE_DEVICES")
            self._set_gpu_env_var(wresources, task, gpus_env)

        return nprocs, nnodes, ppn, extra_args


    #TODO - consider passing resources in when initiaite mpi_runner object
    #TODO - make nprocs, nnodes, ppn a dict to reduce arguments
    #TODO - fix docstring/s in this module
    def get_mpi_specs(
        self, task, nprocs, nnodes, ppn, machinefile, hyperthreads, extra_args,
        auto_assign_gpus, match_procs_to_gpus, resources, workerID
    ):
        "Form the mpi_specs dictionary."

        p_args = None

        # Return auto_resource variables inc. extra_args additions
        if extra_args:
            nprocs, nnodes, ppn, p_args = self._parse_extra_args(
                nprocs, nnodes, ppn, hyperthreads, extra_args=extra_args
            )

        # If no_config_set and auto_assign_gpus - make match_procs_to_gpus default.
        no_config_set = not(nprocs or nnodes or ppn)

        if match_procs_to_gpus:
            jassert(no_config_set, "match_procs_to_gpus is mutually exclusive with any of nprocs/nnodes/ppn")

        if auto_assign_gpus:
            # if no_config_set, make match_procs_to_gpus default.
            if no_config_set:
                match_procs_to_gpus = True
            nprocs, nnodes, ppn, extra_args = self._assign_to_slots(task, resources, nprocs, nnodes, ppn, extra_args, match_procs_to_gpus)

        hostlist = None
        if machinefile and not self.mfile_support:
            logger.warning(f"User machinefile ignored - not supported by {self.run_command}")
            machinefile = None

        if machinefile is None and resources is not None:
            nprocs, nnodes, ppn = mpi_resources.get_resources(
                resources, nprocs, nnodes, ppn, hyperthreads
            )
            hostlist, machinefile = self.express_spec(
                task, nprocs, nnodes, ppn, machinefile, hyperthreads, extra_args, resources, workerID
            )
        else:
            nprocs, nnodes, ppn = mpi_resources.task_partition(
                nprocs, nnodes, ppn, machinefile
            )

        # Remove portable variable if in extra_args
        if extra_args:
            nprocs, nnodes, ppn = self._rm_replicated_args(
                nprocs, nnodes, ppn, p_args
            )

        if self.default_mpi_options is not None:
            extra_args += f" {self.default_mpi_options}"

        return {
            "num_procs": nprocs,
            "num_nodes": nnodes,
            "procs_per_node": ppn,
            "extra_args": extra_args,
            "machinefile": machinefile,
            "hostlist": hostlist,
        }


class MPICH_MPIRunner(MPIRunner):
    def __init__(self, run_command="mpirun", platform_info=None):
        self.run_command = run_command
        self.subgroup_launch = True
        self.mfile_support = True
        self.arg_nprocs = ("-n", "-np")
        self.arg_nnodes = ("--LIBE_NNODES_ARG_EMPTY",)
        self.arg_ppn = ("--ppn",)
        self.default_mpi_options = None
        self.default_gpu_arg = None
        self.default_gpu_arg_type = None
        self.platform_info = platform_info

        self.mpi_command = [
            self.run_command,
            "--env {env}",
            "-machinefile {machinefile}",
            "-hosts {hostlist}",
            "-np {num_procs}",
            "--ppn {procs_per_node}",
            "{extra_args}",
        ]


class OPENMPI_MPIRunner(MPIRunner):
    def __init__(self, run_command="mpirun", platform_info=None):
        self.run_command = run_command
        self.subgroup_launch = True
        self.mfile_support = True
        self.arg_nprocs = ("-n", "-np", "-c", "--n")
        self.arg_nnodes = ("--LIBE_NNODES_ARG_EMPTY",)
        self.arg_ppn = ("-npernode",)
        self.default_mpi_options = None
        self.default_gpu_arg = None
        self.default_gpu_arg_type = None
        self.platform_info = platform_info
        self.mpi_command = [
            self.run_command,
            "-x {env}",
            "-machinefile {machinefile}",
            "-host {hostlist}",
            "-np {num_procs}",
            "-npernode {procs_per_node}",
            "{extra_args}",
        ]

    def express_spec(
        self, task, nprocs, nnodes, ppn, machinefile, hyperthreads, extra_args, resources, workerID
    ):
        hostlist = None
        machinefile = None
        # Use machine files for OpenMPI
        # as "-host" requires entry for every rank

        machinefile = "machinefile_autogen"
        if workerID is not None:
            machinefile += f"_for_worker_{workerID}"
        machinefile += f"_task_{task.id}"
        mfile_created, nprocs, nnodes, ppn = mpi_resources.create_machinefile(
            resources, machinefile, nprocs, nnodes, ppn, hyperthreads
        )
        jassert(mfile_created, "Auto-creation of machinefile failed")

        return hostlist, machinefile


class APRUN_MPIRunner(MPIRunner):
    def __init__(self, run_command="aprun", platform_info=None):
        self.run_command = run_command
        self.subgroup_launch = False
        self.mfile_support = False
        self.arg_nprocs = ("-n",)
        self.arg_nnodes = ("--LIBE_NNODES_ARG_EMPTY",)
        self.arg_ppn = ("-N",)
        self.default_mpi_options = None
        self.default_gpu_arg = None
        self.default_gpu_arg_type = None
        self.platform_info = platform_info
        self.mpi_command = [
            self.run_command,
            "-e {env}",
            "-L {hostlist}",
            "-n {num_procs}",
            "-N {procs_per_node}",
            "{extra_args}",
        ]


class MSMPI_MPIRunner(MPIRunner):
    def __init__(self, run_command="mpiexec", platform_info=None):
        self.run_command = run_command
        self.subgroup_launch = False
        self.mfile_support = False
        self.arg_nprocs = ("-n", "-np")
        self.arg_nnodes = ("--LIBE_NNODES_ARG_EMPTY",)
        self.arg_ppn = ("-cores",)
        self.default_mpi_options = None
        self.default_gpu_arg = None
        self.default_gpu_arg_type = None
        self.platform_info = platform_info
        self.mpi_command = [
            self.run_command,
            "-env {env}",
            "-n {num_procs}",
            "-cores {procs_per_node}",
            "{extra_args}",
        ]


class SRUN_MPIRunner(MPIRunner):
    def __init__(self, run_command="srun", platform_info=None):
        self.run_command = run_command
        self.subgroup_launch = False
        self.mfile_support = False
        self.arg_nprocs = ("-n", "--ntasks")
        self.arg_nnodes = ("-N", "--nodes")
        self.arg_ppn = ("--ntasks-per-node",)
        self.default_mpi_options = "--exact"
        self.default_gpu_arg = "--gpus-per-node="
        self.default_gpu_arg_type = GPU_SET_CLI
        self.platform_info = platform_info
        self.mpi_command = [
            self.run_command,
            "-w {hostlist}",
            "--ntasks {num_procs}",
            "--nodes {num_nodes}",
            "--ntasks-per-node {procs_per_node}",
            "{extra_args}",
        ]


class JSRUN_MPIRunner(MPIRunner):
    def __init__(self, run_command="jsrun", platform_info=None):
        self.run_command = run_command
        self.subgroup_launch = True
        self.mfile_support = False

        # TODO: Add multiplier to resources checks (for -c/-a)
        self.arg_nprocs = ("--np", "-n")
        self.arg_nnodes = ("--LIBE_NNODES_ARG_EMPTY",)
        self.arg_ppn = ("-r",)
        self.default_mpi_options = None
        self.default_gpu_arg = "-g"
        self.default_gpu_arg_type = GPU_SET_CLI_GPT

        self.platform_info = platform_info
        self.mpi_command = [self.run_command, "-n {num_procs}", "-r {procs_per_node}", "{extra_args}"]

    def get_mpi_specs(
        self, task, nprocs, nnodes, ppn, machinefile, hyperthreads, extra_args,
        auto_assign_gpus, match_procs_to_gpus, resources, workerID
    ):
        # Return auto_resource variables inc. extra_args additions

        p_args = None

        if extra_args:
            nprocs, nnodes, ppn, p_args = self._parse_extra_args(
                nprocs, nnodes, ppn, hyperthreads, extra_args=extra_args
            )

        # If no_config_set and auto_assign_gpus - make match_procs_to_gpus default.
        no_config_set = not(nprocs or nnodes or ppn)

        if match_procs_to_gpus:
            jassert(no_config_set, "match_procs_to_gpus is mutually exclusive with any of nprocs/nnodes/ppn")

        if auto_assign_gpus:
            # if no_config_set, make match_procs_to_gpus default.
            if no_config_set:
                match_procs_to_gpus = True
            nprocs, nnodes, ppn, extra_args = self._assign_to_slots(task, resources, nprocs, nnodes, ppn, extra_args, match_procs_to_gpus)

        rm_rpn = True if ppn is None and nnodes is None else False

        hostlist = None
        if machinefile and not self.mfile_support:
            logger.warning(f"User machinefile ignored - not supported by {self.run_command}")
            machinefile = None
        if machinefile is None and resources is not None:
            nprocs, nnodes, ppn = mpi_resources.get_resources(
                resources, nprocs, nnodes, ppn, hyperthreads
            )

            # TODO: Create ERF file if mapping worker to resources req.
        else:
            nprocs, nnodes, ppn = mpi_resources.task_partition(
                nprocs, nnodes, ppn, machinefile
            )

        # Remove portable variable if in extra_args
        if extra_args:
            nprocs, nnodes, ppn = self._rm_replicated_args(
                nprocs, nnodes, ppn, p_args
            )

        if rm_rpn:
            ppn = None

        if self.default_mpi_options is not None:
            extra_args += f" {self.default_mpi_options}"

        return {
            "num_procs": nprocs,
            "num_nodes": nnodes,
            "procs_per_node": ppn,
            "extra_args": extra_args,
            "machinefile": machinefile,
            "hostlist": hostlist,
        }
