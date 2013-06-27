########################################################################
# $HeadURL$
# File :   SSHComputingElement.py
# Author : Dumitru Laurentiu
########################################################################

""" SSH (Virtual) Computing Element: For a given list of ip/cores pair it will send jobs
    directly through ssh
    It's still under development & debugging,
"""

from DIRAC.Resources.Computing.SSHComputingElement       import SSHComputingElement
from DIRAC.Resources.Computing.PilotBundle               import bundleProxy, writeScript
from DIRAC.Core.Utilities.Subprocess                     import shellCall, systemCall
from DIRAC.Core.Utilities.List                           import breakListIntoChunks
from DIRAC.Core.Utilities.Pfn                            import pfnparse
from DIRAC                                               import S_OK, S_ERROR
from DIRAC                                               import rootPath
from DIRAC                                               import gConfig, gLogger
from DIRAC.Core.Security.ProxyInfo                       import getProxyInfo
from DIRAC.Resources.Computing.SSHComputingElement       import SSH 

import os, sys, time, re, socket, stat, shutil
import string, shutil, bz2, base64, tempfile, random

CE_NAME = 'SSHBatch'

class SSHBatchComputingElement( SSHComputingElement ):

  #############################################################################
  def __init__( self, ceUniqueID ):
    """ Standard constructor.
    """
    SSHComputingElement.__init__( self, ceUniqueID )

    self.ceType = CE_NAME
    self.sshHost = []

  def _reset( self ):

    self.queue = self.ceParameters['Queue']
    self.sshScript = os.path.join( rootPath, "DIRAC", "Resources", "Computing", "remote_scripts", "sshce" )
    if 'ExecQueue' not in self.ceParameters or not self.ceParameters['ExecQueue']:
      self.ceParameters['ExecQueue'] = self.ceParameters.get( 'Queue', '' )
    self.execQueue = self.ceParameters['ExecQueue']
    self.log.info( "Using queue: ", self.queue )
    self.hostname = socket.gethostname()
    self.sharedArea = self.ceParameters['SharedArea']
    self.batchOutput = self.ceParameters['BatchOutput']
    if not self.batchOutput.startswith( '/' ):
      self.batchOutput = os.path.join( self.sharedArea, self.batchOutput )
    self.batchError = self.ceParameters['BatchError']
    if not self.batchError.startswith( '/' ):
      self.batchError = os.path.join( self.sharedArea, self.batchError )
    self.infoArea = self.ceParameters['InfoArea']
    if not self.infoArea.startswith( '/' ):
      self.infoArea = os.path.join( self.sharedArea, self.infoArea )
    self.executableArea = self.ceParameters['ExecutableArea']
    if not self.executableArea.startswith( '/' ):
      self.executableArea = os.path.join( self.sharedArea, self.executableArea )
    self.workArea = self.ceParameters['WorkArea']  
    if not self.workArea.startswith( '/' ):
      self.workArea = os.path.join( self.sharedArea, self.workArea )    
      
    # Prepare all the hosts  
    for h in self.ceParameters['SSHHost'].strip().split( ',' ):
      host = h.strip().split('/')[0]
      result = self._prepareRemoteHost( host = host )
      self.log.info( 'Host %s registered for usage' % host )
      self.sshHost.append( h.strip() )

    self.submitOptions = ''
    if 'SubmitOptions' in self.ceParameters:
      self.submitOptions = self.ceParameters['SubmitOptions']
    self.removeOutput = True
    if 'RemoveOutput' in self.ceParameters:
      if self.ceParameters['RemoveOutput'].lower()  in ['no', 'false', '0']:
        self.removeOutput = False

  #############################################################################
  def submitJob( self, executableFile, proxy, numberOfJobs = 1 ):
    """ Method to submit job
    """

    # Choose eligible hosts, rank them by the number of available slots
    rankHosts = {}
    maxSlots = 0
    for host in self.sshHost:
      thost = host.split( "/" )
      hostName = thost[0]
      maxHostJobs = 1
      if len( thost ) > 1:
        maxHostJobs = int( thost[1] )
        
      result = self._getHostStatus( hostName )      
      if not result['OK']:
        continue
      slots = maxHostJobs - result['Value']['Running']
      if slots > 0:
        rankHosts.setdefault(slots,[])
        rankHosts[slots].append( hostName )
      if slots > maxSlots:
        maxSlots = slots

    if maxSlots == 0:
      return S_ERROR( "No online node found on queue" )
    ##make it executable
    if not os.access( executableFile, 5 ):
      os.chmod( executableFile, 0755 )
    
    # if no proxy is supplied, the executable can be submitted directly
    # otherwise a wrapper script is needed to get the proxy to the execution node
    # The wrapper script makes debugging more complicated and thus it is
    # recommended to transfer a proxy inside the executable if possible.
    if proxy:
      self.log.verbose( 'Setting up proxy for payload' )
      wrapperContent = bundleProxy( executableFile, proxy )
      name = writeScript( wrapperContent, os.getcwd() )
      submitFile = name
    else: # no proxy
      submitFile = executableFile

    # Submit jobs now
    restJobs = numberOfJobs
    submittedJobs = []
    for slots in range(maxSlots,0,-1):
      if not slots in rankHosts:
        continue
      for host in rankHosts[slots]:        
        result = self._submitJobToHost( submitFile, min( slots, restJobs ), host )
        if not result['OK']:
          continue
        else:
          nJobs = len( result['Value'] )
          if nJobs > 0:
            submittedJobs.extend( result['Value'] )
            restJobs = restJobs - nJobs
            if restJobs <= 0:
              break
      if restJobs <= 0:
        break      
        
    if proxy:
      os.remove( submitFile )    
            
    return S_OK( submittedJobs )        

  def killJob( self, jobIDs ):
    """ Kill specified jobs
    """ 
    jobIDList = list( jobIDs )
    if type( jobIDs ) == type( ' ' ):
      jobIDList = [jobIDs]
    
    hostDict = {}
    for job in jobIDList:      
      result = pfnparse( job )
      if not result['OK']:
        continue
      host = result['Value']['Host']
      hostDict.setdefault(host,[])
      hostDict[host].append( job )
      
    failed = []  
    for host,jobIDList in hostDict.items():      
      result = self._killJobOnHost( jobIDList, host )
      if not result['OK']:
        failed.extend( jobIDList )
        message = result['Message']
        
    if failed:
      result = S_ERROR(message) 
      result['Failed'] = failed
    else:
      result = S_OK()
      
    return result       

  def getCEStatus( self ):
    """ Method to return information on running and pending jobs.
    """
    result = S_OK()
    result['SubmittedJobs'] = self.submittedJobs
    result['RunningJobs'] = 0
    result['WaitingJobs'] = 0

    for host in self.sshHost:
      thost = host.split( "/" )
      resultHost = self._getHostStatus( thost[0] )     
      if resultHost['OK']:
        result['RunningJobs'] += resultHost['Value']['Running']

    self.log.verbose( 'Waiting Jobs: ', 0 )
    self.log.verbose( 'Running Jobs: ', result['RunningJobs'] )

    return result

  def getJobStatus ( self, jobIDList ):
    """ Get status of the jobs in the given list
    """
    hostDict = {}
    for job in jobIDList:
      result = pfnparse( job )
      if not result['OK']:
        continue
      host = result['Value']['Host']
      hostDict.setdefault(host,[])
      hostDict[host].append( job )

    resultDict = {}
    failed = []  
    for host,jobIDList in hostDict.items():
      result = self._getJobStatusOnHost( jobIDList, host )
      if not result['OK']:
        failed.extend( jobIDList )
        continue
      resultDict.update( result['Value'] ) 
    
    for job in failed:
      if not job in resultDict:
        resultDict[job] = 'Unknown'

    return S_OK( resultDict )

