import numpy as np

from libensemble.alloc_funcs.support import avail_worker_ids, sim_work, gen_work, count_persis_gens


def persistent_aposmm_alloc(W, H, sim_specs, gen_specs, alloc_specs, persis_info):
    """
    This allocation function will give simulation work if possible, but
    otherwise start up to 1 persistent generator.  If all points requested by
    the persistent generator have been returned from the simulation evaluation,
    then this information is given back to the persistent generator.

    :See:
        ``/libensemble/tests/regression_tests/test_6-hump_camel_persistent_uniform_sampling.py``
    """

    Work = {}
    gen_count = count_persis_gens(W)

    # If i is in persistent mode, and any of its calculated values have
    # returned, give them back to i. Otherwise, give nothing to i
    for i in avail_worker_ids(W, persistent=True):
        if sum(H['returned']) < gen_specs['initial_sample_size']:
            # Don't return if the initial sample is not complete
            continue

        gen_inds = (H['gen_worker'] == i)
        returned_but_not_given = np.logical_and(H['returned'][gen_inds], ~H['given_back'][gen_inds])
        if np.any(returned_but_not_given):
            inds_to_give = np.where(returned_but_not_given)[0]

            gen_work(Work, i,
                     sim_specs['in'] + [n[0] for n in sim_specs['out']] + [('sim_id'), ('x_on_cube')],
                     np.atleast_1d(inds_to_give), persis_info[i], persistent=True)

            H['given_back'][inds_to_give] = True

    task_avail = ~H['given']
    for i in avail_worker_ids(W, persistent=False):
        if np.any(task_avail):
            # perform sim evaluations (if they exist in History).
            sim_ids_to_send = np.nonzero(task_avail)[0][0]  # oldest point
            sim_work(Work, i, sim_specs['in'], np.atleast_1d(sim_ids_to_send), persis_info[i])
            task_avail[sim_ids_to_send] = False

        elif gen_count == 0:
            # Finally, call a persistent generator as there is nothing else to do.
            gen_count += 1
            gen_work(Work, i, gen_specs['in'], [], persis_info[i],
                     persistent=True)

    return Work, persis_info
