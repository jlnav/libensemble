#!/usr/bin/env python

"""
Module to launch and control running jobs.

Contains job_controller, job, and inherited classes. A job_controller can
create and manage multiple jobs. The worker or user-side code can issue
and manage jobs using the launch, poll and kill functions. Job attributes
are queried to determine status. Functions are also provided to access
and interrogate files in the job's working directory.

"""

import os
import subprocess
import logging
import signal
import itertools
from libensemble.register import Register

logger = logging.getLogger(__name__)
formatter = logging.Formatter('%(name)s (%(levelname)s): %(message)s')
stream_handler = logging.StreamHandler()
stream_handler.setFormatter(formatter)
logger.addHandler(stream_handler)

#For debug messages - uncomment
logger.setLevel(logging.DEBUG)

STATES = '''
UNKNOWN
CREATED
WAITING
RUNNING
FINISHED
USER_KILLED
FAILED'''.split()

#I may want to use a top-level abstract/base class for maximum re-use
# - else inherited controller will be reimplementing common code

class JobControllerException(Exception): pass


class Job:
    
    '''Manage the creation, configuration and status of a launchable job.'''

    newid = itertools.count()
    
    def __init__(self, app=None, app_args=None, num_procs=None, num_nodes=None, ranks_per_node=None, machinefile=None, workdir = None, stdout = None, workerid = None):
        '''Instantiate a new Job instance.
        
        A new job object is created with an id, status and configuration attributes
        '''
        self.id = next(Job.newid)
                
        #Status attributes
        self.state = 'CREATED'
        self.process = None        
        self.errcode = None
        self.finished = False  # True means job has run - not whether was successful
        self.success = False
        
        #Run attributes
        self.app = app
        self.app_args = app_args      
        self.num_procs = num_procs
        self.num_nodes = num_nodes
        self.ranks_per_node = ranks_per_node
        self.machinefile = machinefile
        self.stdout = stdout
        self.workerID = workerid
        
        if app is not None:
            if self.workerID is not None:
                self.name = 'job_' + app.name + '_worker' + str(self.workerID)  + '_' +  str(self.id)
            else:
                self.name = 'job_' + app.name + '_' + str(self.id)
        else:
            raise JobControllerException("Job must be created with an app - no app found for job ()".format(self.id))
        
        if stdout is not None:
            self.stdout = stdout
        else:
            self.stdout = self.name + '.out'
        
        #self.workdir = './' #Default -  run in place - setting to be implemented
        self.workdir = workdir

    def workdir_exists(self):
        ''' Returns True if the job's workdir exists, else False '''
        if self.workdir is None:
            return False
        if os.path.exists(self.workdir):
            return True
        else:
            return False
        
    def file_exists_in_workdir(self, filename):
        ''' Returns True if the named file exists in the job's workdir, else False '''
        if self.workdir is None:
            return False
        path = os.path.join(self.workdir, filename)
        if os.path.exists(path):
            return True
        else:
            return False 
        
    def read_file_in_workdir(self, filename):
        ''' Open and reads the named file in the job's workdir '''
        path = os.path.join(self.workdir,filename)
        if not os.path.exists(path):
            raise ValueError("%s not found in working directory".format(filename))
        else:
            return open(path).read()
                
    def stdout_exists(self):
        ''' Returns True if the job's stdout file exists in the workdir, else False '''
        if self.workdir is None:
            return False        
        path = os.path.join(self.workdir, self.stdout)
        if os.path.exists(path):
            return True
        else:
            return False
        
    def read_stdout(self):
        ''' Open and reads the job's stdout file in the job's workdir '''
        path = os.path.join(self.workdir, self.stdout)
        if not os.path.exists(path):
            raise ValueError("%s not found in working directory".format(self.stdout))
        else:
            return open(path).read()


class BalsamJob(Job):
    
    '''Wraps a Balsam Job from the Balsam service.'''
    
    #newid = itertools.count() #hopefully can use the one in Job
    
    def __init__(self, app=None, app_args=None, num_procs=None, num_nodes=None, ranks_per_node=None, machinefile=None, workdir = None, stdout = None, workerid = None):
        '''Instantiate a new BalsamJob instance.
        
        A new BalsamJob object is created with an id, status and configuration attributes
        '''
        super().__init__(app, app_args, num_procs, num_nodes, ranks_per_node, machinefile, workdir, stdout, workerid)
        
        self.balsam_state = None
        
        #prob want to override workdir attribute with Balsam value - though does it exist yet?
        self.workdir = None #Don't know until starts running

    def read_file_in_workdir(self, filename):
        out = self.process.read_file_in_workdir(filename)
        return out
    
    def read_stdout(self):
        out = self.process.read_file_in_workdir(self.stdout)
        return out
   

class JobController:
    
    ''' The job_controller can create, poll and kill runnable jobs '''
    
    controller = None
    
    @staticmethod
    def job_partition(num_procs, num_nodes, ranks_per_node, machinefile=None):
        """ Takes provided nprocs/nodes/ranks and outputs working configuration of procs/nodes/ranks or error """
        
        #If machinefile is provided - ignore everything else
        if machinefile is not None:        
            if num_procs is not None or num_nodes is not None or ranks_per_node is not None:
                logger.warning('Machinefile provided - overriding procs/nodes/ranks_per_node')
            num_procs = None
            num_nodes = None
            ranks_per_node = None
            return num_procs, num_nodes, ranks_per_node

        #If all set then check num_procs equals num_nodes*ranks_per_node and set values as given
        if num_procs is not None and num_nodes is not None and ranks_per_node is not None:
            if num_procs != num_nodes*ranks_per_node:
                raise JobControllerException("num_procs does not equal num_nodes*ranks_per_node")
            else:
                return num_procs, num_nodes, ranks_per_node

        #If num_procs not set then need num_nodes and ranks_per_node and set num_procs
        if num_procs is None:
            #Note this covers case where none are set - may want to use job_controller defaults in that case - not implemented yet.
            if num_nodes is None or ranks_per_node is None:
                raise JobControllerException("Must set either num_procs or num_nodes/ranks_per_node or machinefile")
            else:
                num_procs = num_nodes * ranks_per_node
                return num_procs, num_nodes, ranks_per_node
        
        #If num_procs is set - fill in any other values 
        if num_procs is not None:
            if num_nodes is None:
                if ranks_per_node is None:
                    #Currently not auto-detecting so if only num_procs - you are on 1 node
                    num_nodes = 1
                    ranks_per_node = num_procs
                else:
                    num_nodes = num_procs/ranks_per_node
            else:
                ranks_per_node = num_procs/num_nodes
        
        return num_procs, num_nodes, ranks_per_node

    
    def __init__(self, registry=None):
        '''Instantiate a new JobController instance.
        
        A new JobController object is created with an application registry and configuration attributes
        '''
        
        if registry is None:
            self.registry = Register.default_registry #Error handling req.
        else:
            self.registry = registry
        
        if self.registry is None:
            raise JobControllerException("Cannot find default registry")
        
        #Configured possiby by a launcher abstract class/subclasses for launcher type - based on autodetection
        #currently hardcode here - prob prefix with cmd - eg. self.cmd_nprocs
        self.mpi_launcher = 'mpirun'
        self.mfile = '-machinefile'
        self.nprocs = '-np'
        self.nnodes = ''
        self.ppn = '--ppn'
        
        #Job controller settings - can be set in user function.
        self.kill_signal = 'SIGTERM'
        self.wait_and_kill = True #If true - wait for wait_time after signal and then kill with SIGKILL
        self.wait_time = 60
        self.list_of_jobs = []
        self.workerID = None
                
        JobController.controller = self
        
        #If this could share multiple launches could set default job parameters here (nodes/ranks etc...)
        

    # May change job_controller launch functions to use **kwargs and then init job empty - and use setattr
    #eg. To pass through args:
    #def launch(**kwargs):
    #...
    #job = Job()
    #for k,v in kwargs.items():
    #try:
        #getattr(job, k)
    #except AttributeError: 
        #raise ValueError(f"Invalid field {}".format(k)) #Unless not passing through all
    #else:
        #setattr(job, k, v)
    
    def launch(self, calc_type, num_procs=None, num_nodes=None, ranks_per_node=None, machinefile=None, app_args=None, stdout=None, stage_inout=None, test=False):
        ''' Creates a new job, and either launches or schedules to launch in the job controller
        
        The created job object is returned.
        '''
        
        # Find the default sim or gen app from registry.sim_default_app OR registry.gen_default_app
        # Could take optional app arg - if they want to supply here - instead of taking from registry
        if calc_type == 'sim':
            if self.registry.sim_default_app is None:
                raise JobControllerException("Default sim app is not set")
            else:
                app = self.registry.sim_default_app
        elif calc_type == 'gen':
            if self.registry.gen_default_app is not None:
                raise JobControllerException("Default gen app is not set")
            else:
                app = self.registry.gen_default_app
        else:
            raise JobControllerException("Unrecognized calculation type", calc_type)

        
        #-------- Up to here should be common - can go in a baseclass and make all concrete classes inherit ------#
        
        #Set self.num_procs, self.num_nodes and self.ranks_per_node for this job
        num_procs, num_nodes, ranks_per_node = JobController.job_partition(num_procs, num_nodes, ranks_per_node, machinefile)
        
        
        default_workdir = os.getcwd() #Will be possible to override with arg when implemented
        job = Job(app, app_args, num_procs, num_nodes, ranks_per_node, machinefile, default_workdir, stdout, self.workerID)
        
        #Temporary perhaps - though when create workdirs - will probably keep output in place
        if stage_inout is not None:
            logger.warning('stage_inout option ignored in this job_controller - runs in-place')
         
        #Construct run line - possibly subroutine
        runline = []
        runline.append(self.mpi_launcher)
        
        if job.machinefile is not None:
            runline.append(self.mfile)
            runline.append(job.machinefile)
        
        if job.num_procs is not None:
            runline.append(self.nprocs)
            runline.append(str(job.num_procs))
        
        #Not currently setting nodes
        #- as not always supported - but should always have the other two after calling _job_partition
        #if self.num_nodes is not None:
            #runline.append(self.nnodes)
            #runline.append(str(self.num_nodes))
        
        #Currently issues - command depends on mpich/openmpi etc...
        #if self.ranks_per_node is not None:
            #runline.append(self.ppn)
            #runline.append(str(self.ranks_per_node))        

        runline.append(job.app.full_path)
        
        if job.app_args is not None:
            app_args_list = job.app_args.split()
            for iarg in app_args_list:
                runline.append(iarg)
        
        if test:
            print('runline args are', runline)
            print('stdout to', stdout)
            #logger.info(runline)
        else:          
            logger.debug("Launching job: {}".format(" ".join(runline)))
            job.process = subprocess.Popen(runline, cwd='./', stdout = open(job.stdout,'w'), shell=False)
            
            #To test when have workdir
            #job.process = subprocess.Popen(runline, cwd=job.workdir, stdout = open(job.stdout,'w'), shell=False)
            
            self.list_of_jobs.append(job)
        
        #return job.id
        return job

    
    def poll(self, job):
        ''' Polls and updates the status attributes of the supplied job '''
        
        if job is None:
            raise JobControllerException('No job has been provided')

        # Check the jobs been launched (i.e. it has a process ID)
        if job.process is None:
            #logger.warning('Polled job has no process ID - returning stored state')
            #Prob should be recoverable and return state - but currently fatal
            raise JobControllerException('Polled job has no process ID - check jobs been launched')
        
        # Do not poll if job already finished
        # Maybe should re-poll job to check (in case self.finished set in error!)???
        if job.finished:
            logger.warning('Polled job has already finished. Not re-polling. Status is {}'.format(job.state))
            return job
        
        #-------- Up to here should be common - can go in a baseclass and make all concrete classes inherit ------#
        
        # Poll the job
        poll = job.process.poll()
        if poll is None:
            job.state = 'RUNNING'
        else:
            job.finished = True
            #logger.debug("Process {} Completed".format(job.process))
            
            if job.process.returncode == 0:
                job.success = True
                job.errcode = 0
                logger.debug("Process {} completed successfully".format(job.process))
                job.state = 'FINISHED'
            else:
                #Need to differentiate failure from if job was user-killed !!!! What if remotely???
                #If this process killed the job it will already be set and if not re-polling will not get here.
                #But could query existing state here as backup?? - Also may add a REMOTE_KILL state???
                #Not yet remote killing so assume failed....
                job.errcode = job.process.returncode
                job.state = 'FAILED'
        
        #Just updates job as provided
        #return job
                
    
    def kill(self, job):
        ''' Kills or cancels the supplied job '''
        
        if job is None:
            raise JobControllerException('No job has been provided')
        
        #In here can set state to user killed!
        #- but if killed by remote job (eg. through balsam database) may be different .... 

        #Issue signal
        if self.kill_signal == 'SIGTERM':
            job.process.terminate()
        elif self.kill_signal == 'SIGKILL':
            job.process.kill()
        else:
            job.process.send_signal(signal.self.kill_signal) #Prob only need this line!
            
        #Wait for job to be killed
        if self.wait_and_kill:
            try:
                job.process.wait(timeout=self.wait_time)
                #stdout,stderr = self.process.communicate(timeout=self.wait_time) #Wait for process to finish
            except subprocess.TimeoutExpired:
                logger.warning("Kill signal {} timed out - issuing SIGKILL".format(self.kill_signal))
                job.process.kill()
                job.process.wait()
        else:
            job.process.wait(timeout=self.wait_time)

        job.state = 'USER_KILLED'
        job.finished = True
        
        #Need to test out what to do with
        #job.errcode #Can it be discovered after killing?
        #job.success #Could set to false but should be already - only set to true on success            
                
    def set_kill_mode(self, signal=None, wait_and_kill=None, wait_time=None):
        ''' Configures the kill mode for the job_controller '''
        if signal is not None:
            self.kill_signal = signal
            
        if wait_and_kill is not None:
            self.wait_and_kill = wait_and_kill
            
        if wait_time is not None: 
            self.wait_time = wait_time
    
    def get_job(self, jobid):
        ''' Returns the job object for the supplied job ID '''
        if self.list_of_jobs:
            for job in list_of_jobs:
                if job.id == jobid:
                    return job
            logger.warning("Job %s not found in joblist".format(jobid))
            return None
        logger.warning("Job %s not found in joblist. Joblist is empty".format(jobid))
        return None

    def set_workerID(self, workerid):
        self.workerID = workerid

class BalsamJobController(JobController):
    
    '''Inherits from JobController and wraps the Balsam job management service'''
    
    #controller = None
      
    def __init__(self, registry=None):
        '''Instantiate a new BalsamJobController instance.
        
        A new BalsamJobController object is created with an application registry and configuration attributes
        '''        
        
        #Will use super - atleast if use baseclass - but for now dont want to set self.mpi_launcher etc...
        if registry is None:
            self.registry = Register.default_registry #Error handling req.
        else:
            self.registry = registry
        
        if self.registry is None:
            raise JobControllerException("Cannot find default registry")
        
        #-------- Up to here should be common - can go in a baseclass and make all concrete classes inherit ------#
                
        self.list_of_jobs = []
        
        JobController.controller = self
        #BalsamJobController.controller = self
    
    
    def launch(self, calc_type, num_procs=None, num_nodes=None, ranks_per_node=None, machinefile=None, app_args=None, stdout=None, stage_inout=None, test=False):
        ''' Creates a new job, and either launches or schedules to launch in the job controller
        
        The created job object is returned.
        '''        
        import balsam.launcher.dag as dag
        
        # Find the default sim or gen app from registry.sim_default_app OR registry.gen_default_app
        # Could take optional app arg - if they want to supply here - instead of taking from registry
        if calc_type == 'sim':
            if self.registry.sim_default_app is None:
                raise JobControllerException("Default sim app is not set")
            else:
                app = self.registry.sim_default_app
        elif calc_type == 'gen':
            if self.registry.gen_default_app is not None:
                raise JobControllerException("Default gen app is not set")
            else:
                app = self.registry.gen_default_app
        else:
            raise JobControllerException("Unrecognized calculation type", calc_type)
        
        #-------- Up to here should be common - can go in a baseclass and make all concrete classes inherit ------#
        
        #Need test somewhere for if no breakdown supplied.... or only machinefile
        
        #Specific to this class
        if machinefile is not None:
            logger.warning("machinefile arg ignored - not supported in Balsam")
            if num_procs is None and num_nodes is None and ranks_per_node is None:
                raise JobControllerException("No procs/nodes provided - aborting")
            
        
        #Set self.num_procs, self.num_nodes and self.ranks_per_node for this job
        num_procs, num_nodes, ranks_per_node = JobController.job_partition(num_procs, num_nodes, ranks_per_node) #Note: not included machinefile option
        
        #temp - while balsam does not accept a standard out name
        if stdout is not None:
            logger.warning("Balsam does not currently accept a stdout name - ignoring")
            stdout = None
            
        default_workdir = None #Will be possible to override with arg when implemented (else wait for Balsam to assign)
        job = BalsamJob(app, app_args, num_procs, num_nodes, ranks_per_node, machinefile, default_workdir, stdout, self.workerID)
       
        #Re-do debug launch line for balsam job
        #logger.debug("Launching job: {}".format(" ".join(runline)))
        #logger.debug("Added job to Balsam database: {}".format(job.id))
        
        logger.debug("Added job to Balsam database: Worker {} JobID {} nodes {} ppn {}".format(self.workerID, job.id, job.num_nodes, job.ranks_per_node))
        
        if stage_inout is not None:
            #For now hardcode staging - for testing
            job.process = dag.add_job(name = job.name,
                                      workflow = "libe_workflow", #add arg for this
                                      application = app.name,
                                      application_args = job.app_args,                            
                                      num_nodes = job.num_nodes,
                                      ranks_per_node = job.ranks_per_node,
                                      #input_files = app.exe,
                                      stage_in_url = "local:" + stage_inout,
                                      stage_out_url = "local:" + stage_inout,
                                      stage_out_files = "*.out")
                                      #stage_out_files = "*") #Current fails if there are directories
                                      
            #job.process = dag.spawn_child(name = job.name,
                                      #workflow = "libe_workflow", #add arg for this
                                      #application = app.name,
                                      #application_args = job.app_args,                            
                                      #num_nodes = job.num_nodes,
                                      #ranks_per_node = job.ranks_per_node,
                                      ##input_files = app.exe,
                                      #stage_in_url = "local:" + stage_inout,
                                      #stage_out_url = "local:" + stage_inout,
                                      #stage_out_files = "*",
                                      #wait_for_parents=False)            
        else:
            #No staging
            job.process = dag.add_job(name = job.name,
                                      workflow = "libe_workflow", #add arg for this
                                      application = app.name,
                                      application_args = job.app_args,           
                                      num_nodes = job.num_nodes,
                                      ranks_per_node = job.ranks_per_node) 

            #job.process = dag.spawn_child(name = job.name,
                                      #workflow = "libe_workflow", #add arg for this
                                      #application = app.name,
                                      #application_args = job.app_args,           
                                      #num_nodes = job.num_nodes,
                                      #ranks_per_node = job.ranks_per_node,
                                      #input_files = app.exe,
                                      #wait_for_parents=False)
                
        #job.workdir = job.process.working_directory #Might not be set yet!!!!
        self.list_of_jobs.append(job)
        return job

    
    def poll(self, job):
        ''' Polls and updates the status attributes of the supplied job '''
        if job is None:
            raise JobControllerException('No job has been provided') 
        
        # Check the jobs been launched (i.e. it has a process ID)
        if job.process is None:
            #logger.warning('Polled job has no process ID - returning stored state')
            #Prob should be recoverable and return state - but currently fatal
            raise JobControllerException('Polled job has no process ID - check jobs been launched')
        
        # Do not poll if job already finished
        if job.finished:
            logger.warning('Polled job has already finished. Not re-polling. Status is {}'.format(job.state))
            return job 
        
        #-------- Up to here should be common - can go in a baseclass and make all concrete classes inherit ------#
        
        # Get current state of jobs from Balsam database
        job.process.refresh_from_db()
        job.balsam_state = job.process.state #Not really nec to copy have balsam_state - already job.process.state...
        #logger.debug('balsam_state for job {} is {}'.format(job.id, job.balsam_state))
        
        import balsam.launcher.dag as dag #Might need this before get models - test
        from balsam.service import models

        if job.balsam_state in models.END_STATES:
            job.finished = True
            if job.workdir == None:
                job.workdir = job.process.working_directory            
            if job.balsam_state == 'JOB_FINISHED':
                job.success = True
                job.state = 'FINISHED'
            elif job.balsam_state == 'PARENT_KILLED': #I'm not using this currently
                job.state = 'USER_KILLED'
                #job.success = False #Shld already be false - init to false
                #job.errcode = #Not currently returned by Balsam API - requested - else will remain as None
            elif job.balsam_state in STATES: #In my states
                job.state = job.balsam_state
                #job.success = False #All other end states are failrues currently - bit risky
                #job.errcode = #Not currently returned by Balsam API - requested - else will remain as None
            else:
                logger.warning("Job finished, but in unrecognized Balsam state {}".format(job.balsam_state))
                job.state = 'UNKNOWN'
                
        elif job.balsam_state in models.ACTIVE_STATES:
            job.state = 'RUNNING'
            if job.workdir == None:
                job.workdir = job.process.working_directory
            
        elif job.balsam_state in models.PROCESSABLE_STATES + models.RUNNABLE_STATES: #Does this work - concatenate lists
            job.state = 'WAITING'
        else:
            raise JobControllerException('Job state returned from Balsam is not in known list of Balsam states. Job state is {}'.format(job.balsam_state))
        
        #return job
    
    def kill(self, job):
        ''' Kills or cancels the supplied job '''
        import balsam.launcher.dag as dag
        dag.kill(job.process)

        #Could have Wait here and check with Balsam its killed - but not implemented yet.

        job.state = 'USER_KILLED'
        job.finished = True
        
        #Check if can wait for kill to complete - affect signal used etc....
    
    def set_kill_mode(self, signal=None, wait_and_kill=None, wait_time=None):
        ''' Not currently implemented for BalsamJobController'''
        logger.warning("set_kill_mode currently has no action with Balsam controller")
        
