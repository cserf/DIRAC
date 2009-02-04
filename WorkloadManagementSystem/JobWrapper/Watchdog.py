########################################################################
# $Header: /tmp/libdirac/tmp.stZoy15380/dirac/DIRAC3/DIRAC/WorkloadManagementSystem/JobWrapper/Watchdog.py,v 1.39 2009/02/04 18:25:27 paterson Exp $
# File  : Watchdog.py
# Author: Stuart Paterson
########################################################################

"""  The Watchdog class is used by the Job Wrapper to resolve and monitor
     the system resource consumption.  The Watchdog can determine if
     a running job is stalled and indicate this to the Job Wrapper.
     Furthermore, the Watchdog will identify when the Job CPU limit has been
     exceeded and fail jobs meaningfully.

     Information is returned to the WMS via the heart-beat mechanism.  This
     also interprets control signals from the WMS e.g. to kill a running
     job.

     - Still to implement:
          - CPU normalization for correct comparison with job limit
"""

__RCSID__ = "$Id: Watchdog.py,v 1.39 2009/02/04 18:25:27 paterson Exp $"

from DIRAC.Core.Base.Agent                              import Agent
from DIRAC.Core.DISET.RPCClient                         import RPCClient
from DIRAC.ConfigurationSystem.Client.Config            import gConfig
from DIRAC.Core.Utilities.Subprocess                    import shellCall
from DIRAC.Core.Utilities.ProcessMonitor                import ProcessMonitor
from DIRAC                                              import S_OK, S_ERROR
from DIRAC.FrameworkSystem.Client.ProxyManagerClient    import gProxyManager
from DIRAC.Core.Security.Misc                           import getProxyInfo
from DIRAC.Core.Security                                import Properties

import os,thread,time,shutil

AGENT_NAME = 'WorkloadManagement/Watchdog'

class Watchdog(Agent):

  def __init__(self, pid, exeThread, spObject, jobCPUtime, systemFlag='linux2.4'):
    """ Constructor, takes system flag as argument.
    """
    Agent.__init__(self,AGENT_NAME)
    self.systemFlag = systemFlag
    self.exeThread = exeThread
    self.wrapperPID = pid
    self.appPID = self.exeThread.getCurrentPID()
    self.spObject = spObject
    self.jobCPUtime = jobCPUtime
    self.calibration = 0
    self.initialValues = {}
    self.parameters = {}
    self.peekFailCount = 0
    self.peekRetry = 5
    self.processMonitor = ProcessMonitor()
    self.pilotProxyLocation = False
    self.pilotInfo = False

  def setPilotProxyLocation( self, pilotProxyLocation ):
    self.pilotProxyLocation = pilotProxyLocation
    retVal = getProxyInfo( pilotProxyLocation, disableVOMS = True )
    if not retVal[ 'OK' ]:
      self.log.error( "Cannot load pilot proxy %s: %s" % ( pilotProxyLocation, retVal[ 'Message' ] ) )
    self.pilotInfo = retVal[ 'Value' ]
    isGeneric = 'groupProperties' in self.pilotInfo and Properties.GENERIC_PILOT in self.pilotInfo[ 'groupProperties' ]
    self.pilotInfo[ 'GENERIC_PILOT' ] = isGeneric


  #############################################################################
  def initialize(self,loops=0):
    """ Watchdog initialization.
    """
    self.maxcount = loops
    result = Agent.initialize(self)
    if os.path.exists(self.controlDir+'/stop_agent'):
      os.remove(self.controlDir+'/stop_agent')
    self.log.verbose('Watchdog initialization')
    self.log.info('Attempting to Initialize Watchdog for: %s' % (self.systemFlag))
    #Test control flags
    self.testWallClock   = gConfig.getValue(self.section+'/CheckWallClockFlag',1)
    self.testDiskSpace   = gConfig.getValue(self.section+'/CheckDiskSpaceFlag',1)
    self.testLoadAvg     = gConfig.getValue(self.section+'/CheckLoadAvgFlag',1)
    self.testCPUConsumed = gConfig.getValue(self.section+'/CheckCPUConsumedFlag',0)
    self.testCPULimit    = gConfig.getValue(self.section+'/CheckCPULimitFlag',0)
    #Other parameters
    self.pollingTime      = gConfig.getValue(self.section+'/PollingTime',10) # 10 seconds
    self.checkingTime     = gConfig.getValue(self.section+'/CheckingTime',30*60) #30 minute period
    self.minCheckingTime   = gConfig.getValue(self.section+'/MinCheckingTime',20*60) # 20 mins
    self.maxWallClockTime = gConfig.getValue(self.section+'/MaxWallClockTime',4*24*60*60) # e.g. 4 days
    self.jobPeekFlag      = gConfig.getValue(self.section+'/JobPeekFlag',1) # on / off
    self.minDiskSpace     = gConfig.getValue(self.section+'/MinDiskSpace',10) #MB
    self.loadAvgLimit     = gConfig.getValue(self.section+'/LoadAverageLimit',1000) # > 1000 and jobs killed
    self.sampleCPUTime    = gConfig.getValue(self.section+'/CPUSampleTime',30*60) # e.g. up to 20mins sample
    self.jobCPUMargin     = gConfig.getValue(self.section+'/JobCPULimitMargin',20) # %age buffer before killing job
    self.minCPUWallClockRatio  = gConfig.getValue(self.section+'/MinCPUWallClockRatio',5) #ratio %age
    self.nullCPULimit = gConfig.getValue(self.section+'/NullCPUCountLimit',5) #After 5 sample times return null CPU consumption kill job
    self.checkCount = 0
    self.nullCPUCount = 0
    if self.checkingTime < self.minCheckingTime:
      self.log.info('Requested CheckingTime of %s setting to %s seconds (minimum)' %(self.checkingTime,self.minCheckingTime))
      self.checkingTime=self.minCheckingTime
    return result

  #############################################################################
  def execute(self):
    """ The main agent execution method of the Watchdog.
    """
    if not self.exeThread.isAlive():
      #print self.parameters
      self.__getUsageSummary()
      self.log.info('Process to monitor has completed, Watchdog will exit.')
      self.__finish()
      return S_OK()

    #Note: need to poll regularly to see if the thread is alive
    #      but only perform checks with a certain frequency
    if (time.time() - self.initialValues['StartTime']) > self.checkingTime*self.checkCount:
      self.checkCount += 1
      if self.pilotProxyLocation and self.pilotInfo and self.pilotInfo[ 'GENERIC_PILOT' ]:
        self.log.verbose( "Checking proxy...")
        gProxyManager.renewProxy( minLifeTime = gConfig.getValue( '/Security/MinProxyLifeTime', 10800 ),
                                  newProxyLifeTime = gConfig.getValue( '/Security/DefaultProxyLifeTime', 86400 ),
                                  proxyToConnect = self.pilotProxyLocation )
      result = self.__performChecks()
      if not result['OK']:
        self.log.warn('Problem during recent checks')
        self.log.warn(result['Message'])
      return S_OK()
    else:
      #self.log.debug('Application thread is alive: checking count is %s' %(self.checkCount))
      return S_OK()


  #############################################################################
  def __performChecks(self):
    """The Watchdog checks are performed at a different period to the checking of the
       application thread and correspond to the checkingTime.
    """
    self.log.verbose('------------------------------------')
    self.log.verbose('Checking loop starts for Watchdog')
    heartBeatDict = {}
    msg = ''
    result = self.getLoadAverage()
    msg += 'LoadAvg: %s ' % (result['Value'])
    heartBeatDict['LoadAverage'] = result['Value']
    self.parameters['LoadAverage'].append(result['Value'])
    result = self.getMemoryUsed()
    msg += 'MemUsed: %.1f kb ' % (result['Value'])
    heartBeatDict['MemoryUsed'] = result['Value']
    self.parameters['MemoryUsed'].append(result['Value'])
    result = self.getDiskSpace()
    msg += 'DiskSpace: %.1f MB ' % (result['Value'])
    self.parameters['DiskSpace'].append(result['Value'])
    heartBeatDict['AvailableDiskSpace'] = result['Value']
    result = self.__getCPU()
    msg += 'CPU: %s (h:m:s) ' % (result['Value'])
    self.parameters['CPUConsumed'].append(result['Value'])
    hmsCPU = result['Value']
    rawCPU = self.__convertCPUTime(hmsCPU)
    if rawCPU['OK']:
      heartBeatDict['CPUConsumed'] = rawCPU['Value']
    result = self.__getWallClockTime()
    msg += 'WallClock: %.2f s ' % (result['Value'])
    self.parameters['WallClockTime'].append(result['Value'])
    heartBeatDict['WallClockTime'] = result['Value']
    self.log.info(msg)

    result = self.__checkProgress()
    if not result['OK']:
      self.log.warn(result['Message'])
      if self.jobPeekFlag:
        result = self.__peek()
        if result['OK']:
          outputList = result['Value']
          size = len(outputList)
          self.log.info('Last %s lines of available application output:' % (size) )
          self.log.info('================START================')
          for line in outputList:
            self.log.info(line)

          self.log.info('=================END=================')

      self.__killRunningThread(self.spObject)
      self.__getUsageSummary()
      self.__finish()
      return S_OK()

    recentStdOut = 'None'
    if self.jobPeekFlag:
      result = self.__peek()
      if result['OK']:
        outputList = result['Value']
        size = len(outputList)
        recentStdOut = 'Last %s lines of application output from Watchdog on %s [UTC]:' % (size,time.asctime(time.gmtime()))
        border = ''
        for i in xrange(len(recentStdOut)):
          border+='='
        cpuTotal = 'Last reported CPU consumed for job is %s (h:m:s)' %(hmsCPU)
        recentStdOut = '\n%s\n%s\n%s\n%s\n' % (border,recentStdOut,cpuTotal,border)
        self.log.info(recentStdOut)
        for line in outputList:
          self.log.info(line)
          recentStdOut += line+'\n'
      else:
        recentStdOut = 'Watchdog is initializing and will attempt to obtain standard output from application thread'
        self.log.info(recentStdOut)
        self.peekFailCount += 1
        if self.peekFailCount > self.peekRetry:
          self.jobPeekFlag = 0
          self.log.warn('Turning off job peeking for remainder of execution')

    if not os.environ.has_key('JOBID'):
      self.log.info('Running without JOBID so parameters will not be reported')
      return S_OK()

    jobID = os.environ['JOBID']
    staticParamDict = {'StandardOutput':recentStdOut}
    self.__sendSignOfLife(int(jobID),heartBeatDict,staticParamDict)
    return S_OK('Watchdog checking cycle complete')

  #############################################################################
  def __getCPU(self):
    """Uses os.times() to get CPU time and returns HH:MM:SS after conversion.
    """
    cpuTime = 0.0
    try:
      cpuTime = self.processMonitor.getCPUConsumed(self.wrapperPID)
    except Exception,x:
      self.log.warn('Could not determine CPU time consumed with exception')
      self.log.warn(str(x))
      return S_OK(cpuTime) #just return null CPU

    if not cpuTime['OK']:
      self.log.warn('Problem while checking consumed CPU')
      self.log.warn(cpuTime)
      return S_OK(cpuTime) #again return null CPU in this case

    cpuTime = cpuTime['Value']
    self.log.verbose("Raw CPU time consumed (s) = %s" % (cpuTime))
    result = self.__getCPUHMS(cpuTime)
    return result

  #############################################################################
  def __getCPUHMS(self,cpuTime):
    mins, secs = divmod(cpuTime, 60)
    hours, mins = divmod(mins, 60)
    humanTime = '%02d:%02d:%02d' % (hours, mins, secs)
    self.log.verbose('Human readable CPU time is: %s' %humanTime)
    return S_OK(humanTime)

  #############################################################################
  def __interpretControlSignal(self,signalDict):
    """This method is called whenever a signal is sent via the result of
       sending a sign of life.
    """
    self.log.info('Received control signal')
    if type(signalDict) == type({}):
      if signalDict.has_key('Kill'):
        self.log.info('Received Kill signal, stopping job via control signal')
        self.__killRunningThread(self.spObject)
        self.__getUsageSummary()
        self.__finish()
      else:
        self.log.info('The following control signal was sent but not understood by the watchdog:')
        self.log.info(signalDict)
    else:
      self.log.info('Expected dictionary for control signal, received:\n%s' %(signalDict))

    return S_OK()

  #############################################################################
  def __checkProgress(self):
    """This method calls specific tests to determine whether the job execution
       is proceeding normally.  CS flags can easily be added to add or remove
       tests via central configuration.
    """
    report = ''

    if self.testWallClock:
      result = self.__checkWallClockTime()
      report += 'WallClock: OK, '
      if not result['OK']:
        self.log.warn(result['Message'])
        return result
    else:
      report += 'WallClock: NA,'

    if self.testDiskSpace:
      result = self.__checkDiskSpace()
      report += 'DiskSpace: OK, '
      if not result['OK']:
        self.log.warn(result['Message'])
        return result
    else:
      report += 'DiskSpace: NA,'

    if self.testLoadAvg:
      result = self.__checkLoadAverage()
      report += 'LoadAverage: OK, '
      if not result['OK']:
        self.log.warn(result['Message'])
        return result
    else:
      report += 'LoadAverage: NA,'

    if self.testCPUConsumed:
      result = self.__checkCPUConsumed()
      report += 'CPUConsumed: OK, '
      if not result['OK']:
        return result
    else:
      report += 'CPUConsumed: NA,'

    if self.testCPULimit:
      result = self.__checkCPULimit()
      report += 'CPULimit OK. '
      if not result['OK']:
        self.log.warn(result['Message'])
        return result
    else:
      report += 'CPUConsumed: NA.'


    self.log.info(report)
    return S_OK('All enabled checks passed')

  #############################################################################
  def __checkCPUConsumed(self):
    """ Checks whether the CPU consumed by application process is reasonable. This
        method will report stalled jobs to be killed.
    """
    #TODO: test that ok jobs don't die :)
    if self.nullCPUCount > self.nullCPULimit:
      stalledTime = self.nullCPULimit*self.sampleCPUTime
      return S_ERROR('Watchdog identified this job as stalled after no accumulated CPU change in %s seconds' %stalledTime)

    cpuList=[]
    if self.parameters.has_key('CPUConsumed'):
      cpuList = self.parameters['CPUConsumed']
    else:
      return S_ERROR('No CPU consumed in collected parameters')

    #First restrict cpuList to specified sample time interval
    #Return OK if time not reached yet

    interval = self.checkingTime
    sampleTime = self.sampleCPUTime
    iterations = int(sampleTime/interval)
    if len(cpuList) < iterations:
      return S_OK('Job running for less than CPU sample time')
    else:
      self.log.debug(cpuList)
      cut = len(cpuList) - iterations
      cpuList = cpuList[cut:]

    cpuSample = []
    for value in cpuList:
      valueTime = self.__convertCPUTime(value)
      if not valueTime['OK']:
        return valueTime
      cpuSample.append(valueTime['Value'])

    #If total CPU consumed / WallClock for iterations less than X
    #can fail job.

    totalCPUConsumed = cpuSample[-1]
    self.log.info('Cumulative CPU consumed by application process is: %s' % (totalCPUConsumed))
    if len(cpuSample)>1:
      cpuConsumedInterval = cpuSample[-1]-cpuSample[0]  #during last interval
      if not cpuConsumedInterval:
        self.nullCPUCount += 1
        self.log.info('No CPU change detected, counter is at %s' %(self.nullCPUCount))
        return S_OK('No CPU change')

      ratio = float(100*cpuConsumedInterval)/float(sampleTime)
      limit = float( self.minCPUWallClockRatio )
      self.log.info('CPU consumed / Wallclock time ratio during last %s seconds is %.2f percent' % (sampleTime,ratio))
      totalRatio = float(totalCPUConsumed)/float(sampleTime)
      self.log.info('Overall CPU consumed / Wallclock ratio is %.2f percent' %(totalRatio))
      if ratio < limit:
        self.log.info(cpuSample)
        noCPURecordedFlag = True
        for i in cpuSample:
          if i:
            noCPURecordedFlag = False
        if noCPURecordedFlag:
          self.log.info('Watchdog would have identified job as stalled but CPU is consistently zero')
          return S_OK('Watchdog cannot obtain CPU')

        return S_ERROR('Watchdog identified this job as stalled after detecting CPU/Wallclock ratio of %s' %ratio)

    else:
      self.log.info('Insufficient CPU consumed information')
      self.log.verbose(cpuList)

    return S_OK('Job consuming CPU')

  #############################################################################

  def __convertCPUTime(self,cputime):
    """ Method to convert the CPU time as returned from the Watchdog
        instances to the equivalent DIRAC normalized CPU time to be compared
        to the Job CPU requirement.
    """
    cpuValue = 0
    cpuHMS = cputime.split(':')
    for i in xrange(len(cpuHMS)):
      cpuHMS[i] = cpuHMS[i].replace('00','0')

    try:
      hours = float(cpuHMS[0])*60*60
      mins  = float(cpuHMS[1])*60
      secs  = float(cpuHMS[2])
      cpuValue = float(hours+mins+secs)
    except Exception,x:
      self.log.warn(str(x))
      return S_ERROR('Could not calculate CPU time')

    #Normalization to be implemented
    normalizedCPUValue = cpuValue

    result = S_OK()
    result['Value'] = normalizedCPUValue
    self.log.debug('CPU value %s converted to %s' %(cputime,normalizedCPUValue))
    return result

  #############################################################################

  def __checkCPULimit(self):
    """ Checks that the job has consumed more than the job CPU requirement
        (plus a configurable margin) and kills them as necessary.
    """
    consumedCPU = 0
    if self.parameters.has_key('CPUConsumed'):
      consumedCPU = self.parameters['CPUConsumed'][-1]

    consumedCPUDict = self.__convertCPUTime(consumedCPU)
    if consumedCPUDict['OK']:
      currentCPU = consumedCPUDict['Value']
    else:
      return S_OK('Not possible to determine current CPU consumed')

    if consumedCPU:
      limit = self.jobCPUtime + self.jobCPUtime * (self.jobCPUMargin / 100 )
      cpuConsumed = float(currentCPU)
      if cpuConsumed > limit:
        self.log.info('Job has consumed more than the specified CPU limit with an additional %s% margin' % (self.jobCPUMargin))
        return S_ERROR('Job has exceeded maximum CPU time limit')
      else:
        return S_OK('Job within CPU limit')
    elif not wrapperCPU and not currentCPU:
      self.log.verbose('Both initial and current CPU consumed are null')
      return S_OK('CPU consumed is not measurable yet')
    else:
      return S_OK('Not possible to determine CPU consumed')

  #############################################################################
  def __checkDiskSpace(self):
    """Checks whether the CS defined minimum disk space is available.
    """
    if self.parameters.has_key('DiskSpace'):
      availSpace = self.parameters['DiskSpace'][-1]
      if availSpace < self.minDiskSpace:
        self.log.info('Not enough local disk space for job to continue, defined in CS as %s MB' % (self.minDiskSpace))
        return S_ERROR('Job has insufficient disk space to continue')
      else:
        return S_OK('Job has enough disk space available')
    else:
      return S_ERROR('Available disk space could not be established')

  #############################################################################
  def __checkWallClockTime(self):
    """Checks whether the job has been running for the CS defined maximum
       wall clock time.
    """
    if self.initialValues.has_key('StartTime'):
      startTime = self.initialValues['StartTime']
      if time.time() - startTime > self.maxWallClockTime:
        self.log.info('Job has exceeded maximum wall clock time of %s seconds' % (self.maxWallClockTime))
        return S_ERROR('Job has exceeded maximum wall clock time')
      else:
        return S_OK('Job within maximum wall clock time')
    else:
      return S_ERROR('Job start time could not be established')

  #############################################################################
  def __checkLoadAverage(self):
    """Checks whether the CS defined maximum load average is exceeded.
    """
    if self.parameters.has_key('LoadAverage'):
      loadAvg = self.parameters['LoadAverage'][-1]
      if loadAvg > float(self.loadAvgLimit):
        self.log.info('Maximum load average exceeded, defined in CS as %s ' % (self.loadAvgLimit))
        return S_ERROR('Job exceeded maximum load average')
      else:
        return S_OK('Job running with normal load average')
    else:
      return S_ERROR('Job load average not established')

  #############################################################################
  def __peek(self):
    """ Uses ExecutionThread.getOutput() method to obtain standard output
        from running thread via subprocess callback function.
    """
    result = self.exeThread.getOutput()
    if not result['OK']:
      self.log.warn('Could not obtain output from running application thread')
      self.log.warn(result['Message'])

    return result

  #############################################################################
  def calibrate(self):
    """ The calibrate method obtains the initial values for system memory and load
        and calculates the margin for error for the rest of the Watchdog cycle.
    """
    self.__getWallClockTime()
    self.parameters['WallClockTime'] = []

    initialCPU=0.0
    result = self.__getCPU()
    self.log.verbose('CPU consumed %s' %(result))
    if not result['OK']:
      msg = 'Could not establish CPU consumed'
      self.log.warn(msg)
      result = S_ERROR(msg)
      return result

    initialCPU = result['Value']

    self.initialValues['CPUConsumed']=initialCPU
    self.parameters['CPUConsumed'] = []

    result = self.getLoadAverage()
    self.log.verbose('LoadAverage: %s' %(result))
    if not result['OK']:
      msg = 'Could not establish LoadAverage'
      self.log.warn(msg)
      result = S_ERROR(msg)
      return result

    self.initialValues['LoadAverage']=result['Value']
    self.parameters['LoadAverage'] = []

    result = self.getMemoryUsed()
    self.log.verbose('MemUsed: %s' %(result))
    if not result['OK']:
      msg = 'Could not establish MemoryUsed'
      self.log.warn(msg)
      result = S_ERROR(msg)
      return result

    self.initialValues['MemoryUsed']=result['Value']
    self.parameters['MemoryUsed'] = []

    result = self. getDiskSpace()
    self.log.verbose('DiskSpace: %s' %(result))
    if not result['OK']:
      msg = 'Could not establish DiskSpace'
      self.log.warn(msg)
      result = S_ERROR(msg)
      return result

    self.initialValues['DiskSpace']=result['Value']
    self.parameters['DiskSpace'] = []

    result = self.getNodeInformation()
    self.log.verbose('NodeInfo: %s' %(result))
    if not result['OK']:
      msg = 'Could not establish static system information'
      self.log.warn(msg)
      result = S_ERROR(msg)
      return result

    if os.environ.has_key('LSB_JOBID'):
      result['LocalJobID'] = os.environ['LSB_JOBID']
    if os.environ.has_key('PBS_JOBID'):
      result['LocalJobID'] = os.environ['PBS_JOBID']
    if os.environ.has_key('QSUB_REQNAME'):
      result['LocalJobID'] = os.environ['QSUB_REQNAME']

    self.__reportParameters(result,'NodeInformation',True)
    self.__reportParameters(self.initialValues,'InitialValues')

    result = S_OK()
    return result

  #############################################################################
  def __finish(self):
    """Force the Watchdog to complete gracefully.
    """
    self.log.info('Watchdog has completed monitoring of the task')
    if not os.path.exists(self.controlDir):
      try:
        os.makedirs(self.controlDir)
      except Exception,x:
        self.log.error('Watchdog could not create control directory',self.controlDir)
    fd = open(self.controlDir+'/stop_agent','w')
    fd.write('Watchdog Agent Stopped at %s [UTC]' % (time.asctime(time.gmtime())))
    fd.close()

  #############################################################################
  def __getUsageSummary(self):
    """ Returns average load, memory etc. over execution of job thread
    """
    summary = {}
    #CPUConsumed
    if self.parameters.has_key('CPUConsumed'):
      cpuList = self.parameters['CPUConsumed']
      if cpuList:
        hmsCPU = cpuList[-1]
        rawCPU = self.__convertCPUTime(hmsCPU)
        if rawCPU['OK']:
          summary['LastUpdateCPU(s)'] = rawCPU['Value']
      else:
        summary['LastUpdateCPU(s)'] = 'Could not be estimated'
    #DiskSpace
    if self.parameters.has_key('DiskSpace'):
      space = self.parameters['DiskSpace']
      if space:
        value = abs(float(space[-1]) - float(self.initialValues['DiskSpace']))
        if value < 0:
          value = 0
        summary['DiskSpace(MB)'] = value
      else:
        summary['DiskSpace(MB)'] = 'Could not be estimated'
    #MemoryUsed
    if self.parameters.has_key('MemoryUsed'):
      memory = self.parameters['MemoryUsed']
      if memory:
        summary['MemoryUsed(kb)'] = abs(float(memory[-1]) - float(self.initialValues['MemoryUsed']))
      else:
        summary['MemoryUsed(kb)'] = 'Could not be estimated'
    #LoadAverage
    if self.parameters.has_key('LoadAverage'):
      laList = self.parameters['LoadAverage']
      if laList:
        la = 0.0
        for load in laList: la += load
        summary['LoadAverage'] = float(la) / float(len(laList))
      else:
        summary['LoadAverage'] = 'Could not be estimated'

    result = self.__getWallClockTime()
    wallClock = result['Value']
    summary['WallClockTime(s)'] = wallClock

    self.__reportParameters(summary,'UsageSummary',True)

  #############################################################################
  def __reportParameters(self,params,title=None,report=False):
    """Will report parameters for job.
    """
    try:
      parameters = []
      self.log.info('==========================================================')
      if title:
        self.log.info('Watchdog will report %s' % (title))
      else:
        self.log.info('Watchdog will report parameters')
      self.log.info('==========================================================')
      vals = params
      if params.has_key('Value'):
        if vals['Value']:
          vals = params['Value']
      for k,v in vals.items():
        if v:
          self.log.info(str(k)+' = '+str(v))
          parameters.append((k,v))
      if report:
        self.__setJobParamList(parameters)

      self.log.info('==========================================================')
    except Exception,x:
      self.log.warn('Problem while reporting parameters')
      self.log.warn(str(x))

  #############################################################################
  def __getWallClockTime(self):
    """ Establishes the Wall Clock time spent since the Watchdog initialization"""
    result = S_OK()
    if self.initialValues.has_key('StartTime'):
      currentTime = time.time()
      wallClock = currentTime - self.initialValues['StartTime']
      result['Value'] = wallClock
    else:
      self.initialValues['StartTime'] = time.time()
      result['Value'] = 0.0

    return result

  #############################################################################
  def __killRunningThread(self,spObject):
    """ Will kill the running thread process and any child processes."""
    self.log.info('Sending kill signal to application PID %s' %(spObject.child.pid))
    result = spObject.killChild()
    self.log.info('Subprocess.killChild() returned:%s ' %(result))
    return S_OK('Thread killed')

  #############################################################################
  def __sendSignOfLife(self,jobID,heartBeatDict,staticParamDict):
    """ Sends sign of life 'heartbeat' signal and triggers control signal
        interpretation.
    """
    jobReport  = RPCClient('WorkloadManagement/JobStateUpdate',timeout=120)
    result = jobReport.sendHeartBeat(jobID,heartBeatDict,staticParamDict)
    if not result['OK']:
      self.log.warn('Problem sending sign of life')
      self.log.warn(result)

    if result['OK'] and result['Value']:
      self.__interpretControlSignal(result['Value'])

    return result

  #############################################################################
  def __setJobParamList(self,value):
    """Wraps around setJobParameters of state update client
    """
    #job wrapper template sets the jobID variable
    if not os.environ.has_key('JOBID'):
      self.log.info('Running without JOBID so parameters will not be reported')
      return S_OK()
    jobID = os.environ['JOBID']
    jobReport  = RPCClient('WorkloadManagement/JobStateUpdate',timeout=120)
    jobParam = jobReport.setJobParameters(int(jobID),value)
    self.log.verbose('setJobParameters(%s,%s)' %(jobID,value))
    if not jobParam['OK']:
      self.log.warn(jobParam['Message'])

    return jobParam

  #############################################################################
  def getNodeInformation(self):
    """ Attempts to retrieve all static system information, should be overridden in a subclass"""
    methodName = 'getNodeInformation'
    self.log.warn('Watchdog: '+methodName+' method should be implemented in a subclass')
    return S_ERROR('Watchdog: '+methodName+' method should be implemented in a subclass')

  #############################################################################
  def getLoadAverage(self):
    """ Attempts to get the load average, should be overridden in a subclass"""
    methodName = 'getLoadAverage'
    self.log.warn('Watchdog: '+methodName+' method should be implemented in a subclass')
    return S_ERROR('Watchdog: '+methodName+' method should be implemented in a subclass')

  #############################################################################
  def getMemoryUsed(self):
    """ Attempts to get the memory used, should be overridden in a subclass"""
    methodName = 'getMemoryUsed'
    self.log.warn('Watchdog: '+methodName+' method should be implemented in a subclass')
    return S_ERROR('Watchdog: '+methodName+' method should be implemented in a subclass')

  #############################################################################
  def getDiskSpace(self):
    """ Attempts to get the available disk space, should be overridden in a subclass"""
    methodName = 'getDiskSpace'
    self.log.warn('Watchdog: '+methodName+' method should be implemented in a subclass')
    return S_ERROR('Watchdog: '+methodName+' method should be implemented in a subclass')

  #EOF#EOF#EOF#EOF#EOF#EOF#EOF#EOF#EOF#EOF#EOF#EOF#EOF#EOF#EOF#EOF#EOF#EOF#EOF#
