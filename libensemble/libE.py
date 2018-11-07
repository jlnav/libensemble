"""
Main libEnsemble routine
============================================

"""

__all__ = ['libE']

import sys
import logging
import traceback

import numpy as np

from libensemble.history import History
from libensemble.libE_manager import manager_main
from libensemble.libE_worker import worker_main
from libensemble.alloc_funcs.give_sim_work_first import give_sim_work_first
from libensemble.comms.comms import QCommProcess, Timeout
from libensemble.comms.logs import manager_logging_config


logger = logging.getLogger(__name__)
#For debug messages in this module  - uncomment
#logger.setLevel(logging.DEBUG)


def eprint(*args, **kwargs):
    """Print to stderr."""
    print(*args, file=sys.stderr, **kwargs)


def report_manager_exception(hist):
    "Write out exception manager exception to stderr and flush streams."
    eprint(traceback.format_exc())
    eprint("\nManager exception raised .. aborting ensemble:\n")
    eprint("\nDumping ensemble history with {} sims evaluated:\n".
           format(hist.sim_count))
    filename = 'libE_history_at_abort_' + str(hist.sim_count) + '.npy'
    np.save(filename, hist.trim_H())
    sys.stdout.flush()
    sys.stderr.flush()


def libE(sim_specs, gen_specs, exit_criteria,
         persis_info={},
         alloc_specs={'alloc_f': give_sim_work_first,
                      'out':[('allocated', bool)]},
         libE_specs={},
         H0=[]):
    """This is the outer libEnsemble routine.

    We dispatch to different types of worker teams depending on
    the contents of libE_specs.  If 'comm' is a field, we use MPI;
    if 'nthreads' is a field, we use threads; if 'nprocesses' is a
    field, we use multiprocessing.

    If an exception is encountered by the manager or workers, the
    history array is dumped to file and MPI abort is called.

    Parameters
    ----------

    sim_specs: :obj:`dict`

        Specifications for the simulation function
        :doc:`(example)<data_structures/sim_specs>`

    gen_specs: :obj:`dict`

        Specifications for the generator function
        :doc:`(example)<data_structures/gen_specs>`

    exit_criteria: :obj:`dict`

        Tell libEnsemble when to stop a run
        :doc:`(example)<data_structures/exit_criteria>`

    persis_info: :obj:`dict`, optional

        Persistent information to be passed between user functions
        :doc:`(example)<data_structures/persis_info>`

    alloc_specs: :obj:`dict`, optional

        Specifications for the allocation function
        :doc:`(example)<data_structures/alloc_specs>`

    libE_specs: :obj:`dict`, optional

        Specifications for libEnsemble
        :doc:`(example)<data_structures/libE_specs>`

    H0: :obj:`dict`, optional

        A previous libEnsemble history to be prepended to the history in the
        current libEnsemble run
        :doc:`(example)<data_structures/history_array>`

    Returns
    -------

    H: :obj:`dict`

        History array storing rows for each point.
        :doc:`(example)<data_structures/history_array>`
        Dictionary containing persistent info

    persis_info: :obj:`dict`

        Final state of persistent information
        :doc:`(example)<data_structures/persis_info>`

    exit_flag: :obj:`int`

        Flag containing job status: 0 = No errors,
        1 = Exception occured and MPI aborted,
        2 = Manager timed out and ended simulation
    """

    if 'nprocesses' in libE_specs:
        libE_f = libE_local
    else:
        libE_f = libE_mpi

    return libE_f(sim_specs, gen_specs, exit_criteria,
                  persis_info, alloc_specs, libE_specs, H0)


# ==================== MPI version =================================


def comms_abort(comm):
    '''Abort all MPI ranks'''
    comm.Abort(1) # Exit code 1 to represent an abort


def libE_mpi(sim_specs, gen_specs, exit_criteria,
             persis_info, alloc_specs, libE_specs, H0):
    "MPI version of the libE main routine"

    from mpi4py import MPI

    # Fill in default values (e.g. MPI_COMM_WORLD for communicator)
    if 'comm' not in libE_specs:
        libE_specs['comm'] = MPI.COMM_WORLD
    if 'color' not in libE_specs:
        libE_specs['color'] = 0

    comm = libE_specs['comm']
    rank = comm.Get_rank()
    is_master = (rank == 0)

    # Check correctness of inputs
    libE_specs = check_inputs(is_master, libE_specs,
                              alloc_specs, sim_specs, gen_specs,
                              exit_criteria, H0)

    # Run manager or worker code, depending
    if is_master:
        return libE_mpi_manager(comm, sim_specs, gen_specs, exit_criteria,
                                persis_info, alloc_specs, libE_specs, H0)

    # Worker returns a subset of MPI output
    libE_mpi_worker(comm, sim_specs, gen_specs, persis_info, libE_specs)
    H = exit_flag = []
    return [], persis_info, []


def libE_mpi_manager(mpi_comm, sim_specs, gen_specs, exit_criteria, persis_info,
                     alloc_specs, libE_specs, H0):
    "Manager routine run at rank 0."

    from libensemble.comms.mpi import MainMPIComm

    exit_flag = []
    hist = History(alloc_specs, sim_specs, gen_specs, exit_criteria, H0)

    # Lauch worker team
    wcomms = [MainMPIComm(mpi_comm, w) for w in
              range(1, mpi_comm.Get_size())]

    try:
        manager_logging_config(filename='ensemble.log', level=logging.DEBUG)
        persis_info, exit_flag = \
          manager_main(hist, libE_specs, alloc_specs, sim_specs, gen_specs,
                       exit_criteria, persis_info, wcomms)

    except Exception:
        report_manager_exception(hist)
        if libE_specs.get('abort_on_exception', True):
            comms_abort(mpi_comm)
        raise
    else:
        logger.debug("Manager exiting")
        print(len(wcomms), exit_criteria)
        sys.stdout.flush()

    H = hist.trim_H()
    return H, persis_info, exit_flag


def libE_mpi_worker(mpi_comm, sim_specs, gen_specs, persis_info, libE_specs):
    "Worker routine run at ranks > 0."

    from libensemble.comms.mpi import MainMPIComm
    comm = MainMPIComm(mpi_comm)
    worker_main(comm, sim_specs, gen_specs, log_comm=True)
    logger.debug("Worker {} exiting".format(libE_specs['comm'].Get_rank()))



# ==================== Process version =================================


def start_proc_team(nworkers, sim_specs, gen_specs, log_comm=True):
    "Launch a process worker team."
    wcomms = [QCommProcess(worker_main, sim_specs, gen_specs, w, log_comm)
              for w in range(1, nworkers+1)]
    for wcomm in wcomms:
        wcomm.run()
    return wcomms


def kill_proc_team(wcomms, timeout):
    "Join on workers (and terminate forcefully if needed)."
    for wcomm in wcomms:
        try:
            wcomm.result(timeout=timeout)
        except Timeout:
            wcomm.terminate()


def libE_local(sim_specs, gen_specs, exit_criteria,
               persis_info, alloc_specs, libE_specs, H0):
    "Main routine for thread/process launch of libE."

    nworkers = libE_specs['nprocesses']
    libE_specs = check_inputs(True, libE_specs,
                              alloc_specs, sim_specs, gen_specs,
                              exit_criteria, H0)

    exit_flag = []
    hist = History(alloc_specs, sim_specs, gen_specs, exit_criteria, H0)

    # Launch worker team
    wcomms = start_proc_team(nworkers, sim_specs, gen_specs)

    try:
        # Set up logger and run manager
        manager_logging_config(filename='ensemble.log', level=logging.DEBUG)
        persis_info, exit_flag = \
          manager_main(hist, libE_specs, alloc_specs, sim_specs, gen_specs,
                       exit_criteria, persis_info, wcomms)
    except Exception:
        report_manager_exception(hist)
        raise
    else:
        logger.debug("Manager exiting")
        print(nworkers, exit_criteria)
        sys.stdout.flush()
    finally:
        kill_proc_team(wcomms, timeout=libE_specs.get('worker_timeout'))

    H = hist.trim_H()
    return H, persis_info, exit_flag


# ==================== Common input checking =================================


_USER_SIM_ID_WARNING = '\n' + 79*'*' + '\n' + \
"""User generator script will be creating sim_id.
Take care to do this sequentially.
Also, any information given back for existing sim_id values will be overwritten!
So everything in gen_out should be in gen_in!""" + \
'\n' + 79*'*' + '\n\n'


def check_consistent_field(name, field0, field1):
    "Check that new field (field1) is compatible with an old field (field0)."
    assert field0.ndim == field1.ndim, \
      "H0 and H have different ndim for field {}".format(name)
    assert (np.all(np.array(field1.shape) >= np.array(field0.shape))), \
      "H too small to receive all components of H0 in field {}".format(name)


def check_inputs(is_master, libE_specs, alloc_specs, sim_specs, gen_specs,
                 exit_criteria, H0):
    """
    Check if the libEnsemble arguments are of the correct data type contain
    sufficient information to perform a run.
    """

    # Check all the input fields are dicts
    assert isinstance(sim_specs, dict), "sim_specs must be a dictionary"
    assert isinstance(gen_specs, dict), "gen_specs must be a dictionary"
    assert isinstance(libE_specs, dict), "libE_specs must be a dictionary"
    assert isinstance(alloc_specs, dict), "alloc_specs must be a dictionary"
    assert isinstance(exit_criteria, dict), "exit_criteria must be a dictionary"

    # Check for at least one valid exit criterion
    assert len(exit_criteria) > 0, "Must have some exit criterion"
    valid_term_fields = ['sim_max', 'gen_max',
                         'elapsed_wallclock_time', 'stop_val']
    assert all([term_field in valid_term_fields
                for term_field in exit_criteria]), \
                "Valid termination options: " + str(valid_term_fields)

    # Check that sim/gen have 'out' entries
    assert len(sim_specs['out']), "sim_specs must have 'out' entries"
    assert len(gen_specs['out']), "gen_specs must have 'out' entries"

    # If exit on stop, make sure it is something that a sim/gen outputs
    if 'stop_val' in exit_criteria:
        stop_name = exit_criteria['stop_val'][0]
        sim_out_names = [e[0] for e in sim_specs['out']]
        gen_out_names = [e[0] for e in gen_specs['out']]
        assert stop_name in sim_out_names + gen_out_names, \
          "Can't stop on {} if it's not in a sim/gen output".format(stop_name)

    # Handle if gen outputs sim IDs
    from libensemble.libE_fields import libE_fields
    if ('sim_id', int) in gen_specs['out']:
        if is_master:
            print(_USER_SIM_ID_WARNING)
            sys.stdout.flush()
         # Must remove 'sim_id' from libE_fields (it is in gen_specs['out'])
        libE_fields = libE_fields[1:]

    # Set up history -- combine libE_fields and sim/gen/alloc specs
    H = np.zeros(1 + len(H0),
                 dtype=libE_fields + list(set(sim_specs['out'] +
                                              gen_specs['out'] +
                                              alloc_specs.get('out', []))))

    # Sanity check prior history
    if len(H0):
        fields = H0.dtype.names

        # Prior history must contain the fields in new history
        assert set(fields).issubset(set(H.dtype.names)), \
          "H0 contains fields {} not in H.".\
          format(set(fields).difference(set(H.dtype.names)))

        # Prior history cannot contain unreturned points
        assert 'returned' not in fields or np.all(H0['returned']), \
          "H0 contains unreturned points."

        # Check dimensional compatibility of fields
        for field in fields:
            check_consistent_field(field, H0[field], H[field])

    return libE_specs
