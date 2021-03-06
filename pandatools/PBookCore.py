import os
import datetime
import time
import commands
import sys
# tweak sys.path since threading cannot be imported with Athena 15 on SLC5/64
try:
    import threading
except:
    tmpOut = commands.getoutput('unset LD_LIBRARY_PATH; unset PYTHONPATH; /usr/bin/python -c "import sys;print sys.path"')
    try:
        exec "tmpSysPath = %s" % tmpOut.split('\n')[-1]
        sys.path = tmpSysPath + sys.path
    except:
        pass
import threading

from pandatools import PdbUtils
from pandatools import Client
from pandatools import BookConfig
from pandatools import GlobalConfig
from pandatools import PLogger
from pandatools import PsubUtils
from pandatools import PandaToolsPkgInfo

# core class for book keeping
class PBookCore:

    # constructor
    def __init__(self,enforceEnter=False,verbose=False,restoreDB=False):
        # verbose
        self.verbose = verbose
        # restore database
        self.restoreDB = restoreDB
        # initialize database
        PdbUtils.initialzieDB(self.verbose,self.restoreDB)
        # check proxy
        self.gridPassPhrase,self.vomsFQAN = PsubUtils.checkGridProxy(
                '',
                enforceEnter,
                self.verbose,
                useCache=True)
        # map between jobset and jediTaskID
        self.jobsetTaskMap = {}
 


    # synchronize database
    def sync(self):
        # get logger
        tmpLog = PLogger.getPandaLogger()
        tmpLog.info("Synchronizing local repository ...")
        # check proxy
        self.gridPassPhrase,self.vomsFQAN = PsubUtils.checkGridProxy(
                self.gridPassPhrase,
                False,
                self.verbose,
                useCache=True)
        # get nickname
        nickName = PsubUtils.getNickname()
        # set Rucio accounting
        PsubUtils.setRucioAccount(nickName,'pbook',True)
        # get JobIDs in local repository
        localJobIDs = PdbUtils.getListOfJobIDs()
        # get recent JobIDs from panda server
        syncTimeRaw = datetime.datetime.utcnow()
        syncTime = syncTimeRaw.strftime('%Y-%m-%d %H:%M:%S')
        # set sync time for the first attempt
        bookConf = BookConfig.getConfig()
        if self.restoreDB:
            # reset last_synctime to restore database 
            bookConf.last_synctime = ''
        # disable
        self.restoreDB = False
        tmpLog.info("It may take several minutes to restore local repository ...")
        if bookConf.last_synctime == '':
            bookConf.last_synctime = datetime.datetime.utcnow()-datetime.timedelta(days=180)
            bookConf.last_synctime = bookConf.last_synctime.strftime('%Y-%m-%d %H:%M:%S')
        maxTaskID = None
        while True:
            status, jediTaskDicts = Client.getJobIDsJediTasksInTimeRange(bookConf.last_synctime,
                                                                         minTaskID=maxTaskID,
                                                                         verbose=self.verbose)
            if status != 0:
                tmpLog.error("Failed to get tasks from panda server")
                return
            if len(jediTaskDicts) == 0:
                break
            tmpLog.info("Got %s tasks to be updated" % len(jediTaskDicts))
            # insert if missing
            for remoteJobID in jediTaskDicts.keys():
                taskID = jediTaskDicts[remoteJobID]['jediTaskID']
                # get max
                if maxTaskID is None or taskID > maxTaskID:
                    maxTaskID = taskID
                # check local status
                job = None
                if remoteJobID in localJobIDs:
                    # get job info from local repository
                    job = PdbUtils.readJobDB(remoteJobID, self.verbose)
                    # skip if frozen
                    if job.dbStatus == 'frozen':
                        continue
                tmpLog.info("Updating taskID=%s ..." % taskID)
                # convert JEDI task
                localJob = PdbUtils.convertJTtoD(jediTaskDicts[remoteJobID],job)
                # update database
                if not remoteJobID in localJobIDs:
                    # insert to DB
                    try:
                        PdbUtils.insertJobDB(localJob,self.verbose)
                    except:
                        tmpLog.error("Failed to insert taskID=%s to local repository" % taskID)
                        return
                else:
                    # update
                    try:
                        PdbUtils.updateJobDB(localJob,self.verbose,syncTimeRaw)
                    except:
                        tmpLog.error("Failed to update local repository for taskID=%s" % taskID)
                        return
        # update sync time
        bookConf = BookConfig.getConfig()
        bookConf.last_synctime = syncTime
        BookConfig.updateConfig(bookConf)
        self.updateTaskJobsetMap()
        tmpLog.info("Synchronization Completed")
        

    # update task and jobset map
    def updateTaskJobsetMap(self):
        self.jobsetTaskMap = PdbUtils.getJobsetTaskMap()


    # set merge job status
    def setMergeJobStatus(self,job,forceUpdate=False):
        # only whenmerge job generation is active
        if not forceUpdate and not job.activeMergeGen():
            return True
        # get logger
        tmpLog = PLogger.getPandaLogger()
        # check status of merge job generation
        status,genStauts = Client.checkMergeGenerationStatus(job.JobID,verbose=self.verbose)
        if status != 0:
            tmpLog.error(genStauts)
            tmpLog.error("Failed to check status of merge job generation for JobID=%s" % job.JobID)
            return False
        # set status
        job.mergeJobStatus = genStauts['status']
        # set merge job IDs
        if genStauts['mergeIDs'] != []:
            job.mergeJobID = ''
            for tmpID in genStauts['mergeIDs']:
                job.mergeJobID += '%s,' % tmpID
            job.mergeJobID = job.mergeJobID[:-1]
        # return
        return True


    # get local job info
    def getJobInfo(self,JobID):
        # get logger
        tmpLog = PLogger.getPandaLogger()
        # convert taskID to jobsetID
        JobID = self.convertTaskToJobID(JobID)
        # get job info from local repository
        job = PdbUtils.readJobDB(JobID,self.verbose)
        # not found
        if job == None:
            tmpLog.warning("JobID=%s not found in local repository. Synchronization may be needed" % JobID)
            return None
        # return
        return job


    # get local job/jobset info
    def getJobJobsetInfo(self,id):
        # get logger
        tmpLog = PLogger.getPandaLogger()
        # try to get jobset
        job = PdbUtils.readJobsetDB(id,self.verbose)
        # get job info from local repository
        if job == None:
            job = PdbUtils.readJobDB(id,self.verbose)
        # not found
        if job == None:
            tmpLog.warning("JobsetID/JobID=%s not found in local repository. Synchronization may be needed" % JobID)
            return None
        # return
        return job


    # get local job list
    def getLocalJobList(self):
        # get jobs
        localJobs = PdbUtils.bulkReadJobDB(self.verbose)
        # return
        return localJobs


    # get JobIDs with JobsetID
    def getJobIDsWithSetID(self,jobsetID):
        # convert taskID to jobsetID
        jobsetID = self.convertTaskToJobID(jobsetID)
        idMap = PdbUtils.getMapJobsetIDJobIDs(self.verbose)
        if idMap.has_key(jobsetID):
            return idMap[jobsetID]
        return None


    # make JobSetSpec
    def makeJobsetSpec(self,jobList):
        return PdbUtils.makeJobsetSpec(jobList)


    # get status
    def status(self,JobID,forceUpdate=False):
        # get logger
        tmpLog = PLogger.getPandaLogger()
        # check proxy
        self.gridPassPhrase,self.vomsFQAN = PsubUtils.checkGridProxy(
                self.gridPassPhrase,
                False,
                self.verbose,
                useCache=True)
        # get job info from local repository
        job = self.getJobInfo(JobID)
        if job == None:
      # not found
            return None
        # update if needed
        if job.dbStatus != 'frozen' or forceUpdate:
            if not job.isJEDI():
                tmpLog.info("Getting status for JobID=%s ..." % JobID)
                # get status from Panda server
                status,pandaIDstatus = Client.getPandIDsWithJobID(JobID,verbose=self.verbose)
                if status != 0:
                    tmpLog.error("Failed to get status for ID=%s" % JobID)
                    return None
                # get one job to set computingSite which may have changed due to rebrokerage
                pandaJob = None
                if pandaIDstatus != {}:
                    tmpPandaIDs = pandaIDstatus.keys()
                    tmpPandaIDs.sort()
                    status,tmpPandaJobs = Client.getFullJobStatus(
                            tmpPandaIDs[:1],
                            verbose=self.verbose)
                    if status != 0:
                        tmpLog.error("Failed to get PandaJobs for %s" % JobID)
                        return None
                    pandaJob = tmpPandaJobs[0]
                # convert to local job spec
                job = PdbUtils.convertPtoD([],pandaIDstatus,job,pandaJobForSiteID=pandaJob)
                # check merge job generation
                status = self.setMergeJobStatus(job,forceUpdate)
                if not status:
                    return None
            else:
                tmpLog.info("Getting status for TaskID=%s ..." % job.jediTaskID)
                # get JEDI task
                status,jediTaskDict = Client.getJediTaskDetails(
                        {'jediTaskID':job.jediTaskID},
                        False,
                        True,
                        verbose=self.verbose)
                if status != 0:
                    tmpLog.error("Failed to get task details for %s" % JobID)
                    return
                # convert JEDI task
                job = PdbUtils.convertJTtoD(jediTaskDict,job)
            # update DB
            try:
                PdbUtils.updateJobDB(job,self.verbose)
            except:
                tmpLog.error("Failed to update local repository for JobID=%s" % JobID)
                return None
            if not job.isJEDI():
                tmpLog.info("Updated JobID=%s" % JobID)                        
            else:
                tmpLog.info("Updated TaskID=%s ..." % job.jediTaskID)
        # return
        return job


    # get status for JobSet and Job
    def statusJobJobset(self,id,forceUpdate=False):
        tmpJobIDList = self.getJobIDsWithSetID(id)
        if tmpJobIDList == None:
            # not a jobset
            job = self.status(id,forceUpdate)
        else:
            # jobset
            tmpJobs = []
            tmpMergeIdList = []
            isJEDI = False
            for tmpJobID in tmpJobIDList:
                tmpJob = self.status(tmpJobID,forceUpdate)
                if tmpJob == None:
                    return None
                tmpJobs.append(tmpJob)
                if tmpJob.isJEDI():
                    isJEDI = True
                else:
                    if tmpJob.mergeJobID != '':
                        for tmpMergeID in tmpJob.mergeJobID.split(','):
                            tmpMergeIdList.append(long(tmpMergeID))
            if not isJEDI:
                # check merge jobs are already got
                tmpIDtoBeChecked = []  
                for tmpMergeID in tmpMergeIdList:
                    if not tmpMergeID in tmpJobIDList:
                        tmpIDtoBeChecked.append(tmpMergeID)
                # sync to get merge job info        
                if tmpIDtoBeChecked != []:
                    self.sync()
                    for tmpJobID in tmpIDtoBeChecked:
                        tmpJob = self.status(tmpJobID,forceUpdate)
                        tmpJobs.append(tmpJob)
            # make jobset
            job = self.makeJobsetSpec(tmpJobs)
        # return     
        return job
    

    # kill
    def kill(self,JobID,useJobsetID=False):
        # get logger
        tmpLog = PLogger.getPandaLogger()
        # check proxy
        self.gridPassPhrase,self.vomsFQAN = PsubUtils.checkGridProxy(
                self.gridPassPhrase,
                False,
                self.verbose,
                useCache=True)
        # force update just in case
        self.status(JobID,True)
        # get jobset
        jobList = self.getJobIDsWithSetID(JobID)
        if jobList == None:
            # works only for jobsetID
            if useJobsetID:
                return
            # works with jobID
            jobList = [JobID]
        else:
            tmpMsg = "ID=%s is composed of JobID=" % JobID
            for tmpJobID in jobList:
                tmpMsg += '%s,' % tmpJobID
            tmpMsg = tmpMsg[:-1]
            tmpLog.info(tmpMsg)
        for tmpJobID in jobList:    
            # get job info from local repository
            job = self.getJobInfo(tmpJobID)
            if job == None:
                tmpLog.warning("JobID=%s not found in local repository. Synchronization may be needed" % tmpJobID)            
                continue
            # skip frozen job
            if job.dbStatus == 'frozen':
                tmpLog.info('All subJobs in JobID=%s already finished/failed' % tmpJobID)
                continue
            if not job.isJEDI():
                # get PandaID list
                killJobs = job.PandaID.split(',')
                # kill
                tmpLog.info('Sending kill command ...')
                status,output = Client.killJobs(killJobs,self.verbose)
                if status != 0:
                    tmpLog.error(output)
                    tmpLog.error("Failed to kill JobID=%s" % tmpJobID)
                    return False
                # update database
                job.commandToPilot = 'tobekilled'
                # update DB
                try:
                    PdbUtils.updateJobDB(job,self.verbose)
                except:
                    tmpLog.error("Failed to update local repository for JobID=%s" % tmpJobID)
                    return False
            else:
                # kill JEDI task
                tmpLog.info('Sending killTask command ...')
                status,output = Client.killTask(job.jediTaskID,self.verbose)
                # communication error
                if status != 0:
                    tmpLog.error(output)
                    tmpLog.error("Failed to kill JobID=%s" % tmpJobID)
                    return False
                tmpStat,tmpDiag = output
                if not tmpStat:
                    tmpLog.error(tmpDiag)
                    tmpLog.error("Failed to kill JobID=%s" % tmpJobID)
                    return False
                tmpLog.info(tmpDiag)
            # done
            if job.isJEDI():
                tmpLog.info('Done. TaskID=%s will be killed in 30min' % job.jediTaskID)
            else:
                tmpLog.info('Done. JobID=%s will be killed in 30min' % tmpJobID)
        return True


    # finish
    def finish(self,JobID,soft=False):
        # get logger
        tmpLog = PLogger.getPandaLogger()
        # check proxy
        self.gridPassPhrase,self.vomsFQAN = PsubUtils.checkGridProxy(
                self.gridPassPhrase,
                False,
                self.verbose,
                useCache=True)
        # force update just in case
        self.status(JobID,True)
        # get jobset
        jobList = self.getJobIDsWithSetID(JobID)
        if jobList == None:
            # works only for jobsetID
            if useJobsetID:
                return
            # works with jobID
            jobList = [JobID]
        else:
            tmpMsg = "ID=%s is composed of JobID=" % JobID
            for tmpJobID in jobList:
                tmpMsg += '%s,' % tmpJobID
            tmpMsg = tmpMsg[:-1]
            tmpLog.info(tmpMsg)
        for tmpJobID in jobList:    
            # get job info from local repository
            job = self.getJobInfo(tmpJobID)
            if job == None:
                tmpLog.warning("JobID=%s not found in local repository. Synchronization may be needed" % tmpJobID)            
                continue
            # skip frozen job
            if job.dbStatus == 'frozen':
                tmpLog.info('All subJobs in JobID=%s already finished/failed' % tmpJobID)
                continue
            # finish JEDI task
            tmpLog.info('Sending finishTask command ...')
            status,output = Client.finishTask(job.jediTaskID,soft,self.verbose)
            # communication error
            if status != 0:
                tmpLog.error(output)
                tmpLog.error("Failed to finish JobID=%s" % tmpJobID)
                return False
            tmpStat,tmpDiag = output
            if not tmpStat:
                tmpLog.error(tmpDiag)
                tmpLog.error("Failed to finish JobID=%s" % tmpJobID)
                return False
            tmpLog.info(tmpDiag)
        # done
        tmpLog.info('Done. TaskID=%s will be finished soon' % job.jediTaskID)
        return True


    # rebrokerage
    def rebrokerage(self,JobsetID,cloud):
        # get logger
        tmpLog = PLogger.getPandaLogger()
        # check proxy
        self.gridPassPhrase,self.vomsFQAN = PsubUtils.checkGridProxy(
                self.gridPassPhrase,    
                False,
                self.verbose,
                useCache=True)
        # get jobset
        jobList = self.getJobIDsWithSetID(JobsetID)
        if jobList == None:
            jobList = [JobsetID]
        else:
            tmpMsg = "JobsetID=%s is composed of JobID=" % JobsetID
            for tmpJobID in jobList:
                tmpMsg += '%s,' % tmpJobID
            tmpMsg = tmpMsg[:-1]
            tmpLog.info(tmpMsg)
        for JobID in jobList:    
            # get job info using status
            job = self.status(JobID)
            if job == None:
                # not found
                continue
            # skip frozen job
            if job.dbStatus == 'frozen':
                tmpLog.info('All subJobs in JobID=%s already finished/failed' % JobID)
                continue
        # rebrokerage
        tmpLog.info('Sending rebrokerage request ...')
        status,output = Client.runReBrokerage(JobID,job.libDS,cloud,self.verbose)
        if status != 0:
            tmpLog.error(output)
            tmpLog.error("Failed to reassign JobID=%s" % JobID)
            return
        # done
        tmpLog.info('Done for %s' % JobID)
        return


    # set debug mode
    def debug(self,pandaID,modeOn):
        # get logger
        tmpLog = PLogger.getPandaLogger()
        # check proxy
        self.gridPassPhrase,self.vomsFQAN = PsubUtils.checkGridProxy(
            self.gridPassPhrase,
            False,
            self.verbose,
            useCache=True)
        # rebrokerage
        status,output = Client.setDebugMode(pandaID,modeOn,self.verbose)
        if status != 0:
            tmpLog.error(output)
            tmpLog.error("Failed to set debug mode for %s" % pandaID)
            return
        # done
        tmpLog.info(output)
        return


    # clean
    def clean(self,nDays=180):
        # get logger
        tmpLog = PLogger.getPandaLogger()
        # delete
        try:
            PdbUtils.deleteOldJobs(nDays,self.verbose)
        except:
            tmpLog.error("Failed to delete old jobs")
            return
        # done
        tmpLog.info('Done')
        return


    # kill and retry
    def killAndRetry(self,JobID,newSite=False,newOpts={},ignoreDuplication=False,retryBuild=False):
        # get logger
        tmpLog = PLogger.getPandaLogger()
        # kill
        retK = self.kill(JobID)
        if not retK:
            return False
        # sleep
        tmpLog.info('Going to sleep for 5sec')
        time.sleep(5)
        nTry = 6
        for iTry in range(nTry):
            # get status
            job = self.status(JobID)
            if job == None:
                return False
            # check if frozen
            if job.dbStatus == 'frozen':
                break
            tmpLog.info('Some sub-jobs are still running')
            if iTry+1 < nTry:
                # sleep
                tmpLog.info('Going to sleep for 10min')
                time.sleep(600)
            else:
                tmpLog.info('Max attempts exceeded. Please try later')
                return False
        # retry
        self.retry(
            JobID,
            newSite=newSite,
            newOpts=newOpts,
            ignoreDuplication=ignoreDuplication,
            retryBuild=retryBuild)
        return
                        

    # retry
    def retry(self,JobsetID,newSite=False,newOpts={},noSubmit=False,ignoreDuplication=False,useJobsetID=False,retryBuild=False,reproduceFiles=[],unsetRetryID=False):
        # get logger
        tmpLog = PLogger.getPandaLogger()
        # check proxy
        self.gridPassPhrase,self.vomsFQAN = PsubUtils.checkGridProxy(
            self.gridPassPhrase,
            False,
            self.verbose,
            useCache=True)
  # force update just in case
        self.status(JobsetID,True)
        # set an empty map since mutable default value is used
        if newOpts == {}:
            newOpts = {}
        # get jobset
        newJobsetID = -1
        jobList = self.getJobIDsWithSetID(JobsetID)
        if jobList == None:
            # works only for jobsetID
            if useJobsetID:
                return
            # works with jobID   
            isJobset = False
            jobList = [JobsetID]
        else:
            isJobset = True
            tmpMsg = "ID=%s is composed of JobID=" % JobsetID
            for tmpJobID in jobList:
                tmpMsg += '%s,' % tmpJobID
            tmpMsg = tmpMsg[:-1]
            tmpLog.info(tmpMsg)
        for JobID in jobList:    
            # get job info from local repository
            localJob = self.getJobInfo(JobID)
            if localJob == None:
                tmpLog.warning("JobID=%s not found in local repository. Synchronization may be needed" % JobID)            
                return None
            # for JEDI
            if localJob.isJEDI():
                status,out = Client.retryTask(
                        localJob.jediTaskID,
                        verbose=self.verbose,
                        properErrorCode=True,
                        newParams=newOpts)
                if status != 0:
                    tmpLog.error(status)
                    tmpLog.error(out)
                    tmpLog.error("Failed to retry TaskID=%s" % localJob.jediTaskID)
                    return False
                tmpStat,tmpDiag = out
                if (not tmpStat in [0,True] and newOpts == {}) or (newOpts != {} and tmpStat != 3):
                    tmpLog.error(tmpDiag)
                    tmpLog.error("Failed to retry TaskID=%s" % localJob.jediTaskID)
                    return False
                tmpLog.info(tmpDiag)
                continue
            # skip running job
            if localJob.dbStatus != 'frozen':
                tmpLog.info('Retry failed subjobs in running jobId=%s' % JobID)
                status,out = Client.retryFailedJobsInActive(JobID,verbose=self.verbose)
                if status != 0:
                    tmpLog.error(status)
                    tmpLog.error(out)
                    tmpLog.error("Failed to retry JobID=%s" % JobID)
                else:
                    job = self.status(JobID)
                if isJobset:
                    continue
                else:
                    return
            # skip already retried
            if localJob.retryID != '0':
                if isJobset:
                    tmpLog.info('Skip JobID=%s since already retried by JobID=%s JobsetID=%s' % \
                                (JobID,localJob.retryID,localJob.retryJobsetID))
                    continue
                else:
                    tmpLog.warning('This job was already retried by JobID=%s' % localJob.retryID)
                    return
            # check status of buildJob
            if not retryBuild and not localJob.buildStatus in ['','finished']:
                tmpMsgStr = 'Cannot retry since status of buildJob %s is %s (!= finished). ' \
                            % (localJob.PandaID.split(',')[0],localJob.buildStatus)
                tmpMsgStr += 'Please execute %s with the same input/output datasets (or containers). ' % localJob.jobType
                tmpMsgStr += 'It will run only on failed/cancelled/unused input files '
                tmpMsgStr += 'and append output files to the output dataset container. '
                tmpMsgStr += 'Or you may set retryBuild=True in pbook.retry() '                
                tmpLog.warning(tmpMsgStr)
                if isJobset:
                    continue
                else:
                    return
            # check opts for newSite
            if newSite or newOpts != {}:
                if not localJob.outDS.endswith('/') and not newOpts.has_key('outDS') and not newOpts.has_key('--outDS'):
                    tmpLog.warning('You need to specify --outDS in newOpts to retry at new site unless container is used as output')
                    return
            # get list of failed jobs
            pandaIDs  = localJob.PandaID.split(',')
            statusList= localJob.jobStatus.split(',')
            jobList = []
            for idx in range(len(pandaIDs)):
                # check status unless reproduce files
                if reproduceFiles == [] and not statusList[idx] in ['failed','cancelled']:
                    continue
                jobList.append(pandaIDs[idx])
            # no failed job
            if jobList == []:
                if isJobset:
                    tmpLog.info('Skip JobID=%s since no failed jobs' % JobID)                    
                    continue
                else:
                    tmpLog.info('No failed jobs to be retried for JobID=%s' % JobID)
                    return
            # get full job spec
            tmpLog.info("Retrying JobID=%s ..." % JobID)
            tmpLog.info("Getting job info")
            idxJL  = 0
            nQuery = 500
            pandaJobs = []
            while idxJL < len(jobList):
                # avoid burst query
                tmpLog.info(" %5s/%s" % (idxJL,len(jobList)))                
                status,oTmp = Client.getFullJobStatus(
                        jobList[idxJL:idxJL+nQuery],
                        verbose=self.verbose)
                if status != 0:
                    tmpLog.error(status)
                    tmpLog.error(oTmp)
                    tmpLog.error("Cannot get job info from Panda server")
                    return
                pandaJobs += oTmp
                idxJL += nQuery
                time.sleep(1)
            tmpLog.info(" %5s/%s" % (len(jobList),len(jobList)))
            # get PandaIDs to reproduce files
            if reproduceFiles != []:
                # change wildcard to .* for regexp
                reproduceFilePatt = []
                for tmpReproduceFile in reproduceFiles:
                    if '*' in tmpReproduceFile:
                        tmpReproduceFile = tmpReproduceFile.replace('*','.*')
                    reproduceFilePatt.append(tmpReproduceFile)
                # get list of jobs which produced interesting files    
                tmpJobList = []
                tmpPandaJobs = []
                for tmpPandaJob in pandaJobs:
                    # check names
                    tmpMatchFlag = False
                    for tmpFile in tmpPandaJob.Files:
                        if tmpFile.type == 'output' and tmpFile.status == 'ready':
                            for tmpReproduceFile in reproduceFilePatt:
                                # normal matching
                                if tmpReproduceFile == tmpFile.lfn:
                                    tmpMatchFlag = True
                                    break
                                # wild card
                                if '*' in tmpReproduceFile and \
                                   re.search('^'+tmpReproduceFile,tmpFile.lfn) != None:
                                    tmpMatchFlag = True
                                    break
                            if tmpMatchFlag:
                                break
                    # append
                    if tmpMatchFlag:
                        tmpJobList.append(tmpPandaJob.PandaID)
                        tmpPandaJobs.append(tmpPandaJob)
                # use new list
                jobList = tmpJobList
                pandaJobs = tmpPandaJobs
                if jobList == []:
                    tmpLog.info("No jobs to reproduce files : Jobs in JobID=%s didn't produce lost files" % JobID)
                    continue
            # jobdefID
            newJobdefID = PsubUtils.readJobDefID()
            # reset some parameters
            retryJobs    = []
            retrySite    = None
            retryElement = None
            retryDestSE  = None
            outDsName    = None
            shadowList   = []
            oldLibDS     = None
            newLibDS     = None
            newLibTgz    = None
            rebroMap     = {}
            for idx in range(len(jobList)):
                job = pandaJobs[idx]
                # skip exired
                if job == None:
                    tmpLog.warning("Could not retry jobs older than 30 days : JobID=%s (PandaID=%s) expired" \
                                   % (JobID,jobList[idxJob]))
                    return
                # skip jobs reassigned by rebrokerage
                if (job.jobStatus == 'cancelled' and job.taskBufferErrorCode in [105,'105']) or \
                       (job.jobStatus == 'failed' and job.taskBufferErrorCode in [106,'106']):
                    # extract JobIDs of reassigned jobs
                    tmpM = re.search('JobsetID=(\d+) JobID=(\d+)',job.taskBufferErrorDiag)
                    if tmpM != None:
                        tmpRebKey = (tmpM.group(1),tmpM.group(2))
                        if not rebroMap.has_key(tmpRebKey):
                            rebroMap[tmpRebKey] = 0
                        # count # of reassigned jobs
                        rebroMap[tmpRebKey] += 1
                    continue
                # get shadow list
                if (not ignoreDuplication) and outDsName == None and job.prodSourceLabel == 'user':
                    # look for dataset for log since it doesn't have suffix even when --individualOutDS is used
                    for tmpFile in job.Files:
                        if tmpFile.type == 'log':
                            outDsName = tmpFile.dataset
                            break
                    # output dataset was not found    
                    if outDsName == None:
                        tmpLog.error("Could not get output dataset name for JobID=%s (PandaID=%s)" \
                                     % (JobID,job.PandaID))
                        return
                    # get files in shadow
                    if outDsName.endswith('/'):
                        shadowList = Client.getFilesInShadowDataset(
                                outDsName,
                                Client.suffixShadow,
                                self.verbose)
                    else:
                        # disable duplication check mainly for old overlay jobs since non-signal files are wrongly skipped
                        #shadowList = Client.getFilesInShadowDatasetOld(outDsName,Client.suffixShadow,self.verbose)
                        pass
                # unify sitename
                if retrySite == None:
                    retrySite    = job.computingSite
                    retryElement = job.computingElement
                    retryDestSE  = job.destinationSE
                # reset
                job.jobStatus           = None
                job.commandToPilot      = None
                job.startTime           = None
                job.endTime             = None
                job.attemptNr           = 1+job.attemptNr
                for attr in job._attributes:
                    if attr.endswith('ErrorCode') or attr.endswith('ErrorDiag'):
                        setattr(job,attr,None)
                job.transExitCode       = None
                job.computingSite       = retrySite
                job.computingElement    = retryElement
                job.destinationSE       = retryDestSE
                job.dispatchDBlock      = None
                if not unsetRetryID:
                    job.jobExecutionID  = JobID
                job.jobDefinitionID     = newJobdefID
                job.parentID            = job.PandaID
                if job.jobsetID != ['NULL',None,-1]:
                    if not unsetRetryID:
                        job.sourceSite  = job.jobsetID
                    job.jobsetID        = newJobsetID
                skipInputList = []
                numUsedFiles = 0
                # loop over all files    
                for file in job.Files:
                    file.rowID = None
                    if file.type == 'input':
                        # protection against wrong sync which doesn't update buildStatus correctly
                        if not retryBuild and file.lfn.endswith('.lib.tgz') and file.GUID == 'NULL':
                            tmpLog.warning('GUID for %s is unknown. Cannot retry when corresponding buildJob failed' \
                                           % file.lfn)
                            return
                        if not retryBuild or not file.lfn.endswith('.lib.tgz'):
                            file.status = 'ready'
                        # set new lib dataset    
                        if retryBuild and file.lfn.endswith('.lib.tgz'):
                            if newLibTgz != None:
                                file.lfn            = newLibTgz
                                file.dataset        = newLibDS
                                file.dispatchDBlock = newLibDS
                        # check with shadow for non lib.tgz/DBR 
                        tmpDbrMatch = re.search('^DBRelease-.*\.tar\.gz$',file.lfn)
                        if tmpDbrMatch == None and not file.lfn.endswith('.lib.tgz'):
                            if file.lfn in shadowList:
                                skipInputList.append(file)
                            else:
                                numUsedFiles += 1
                    elif file.type in ('output','log'):
                        file.destinationSE = retryDestSE
                        file.destinationDBlock = re.sub('_sub\d+$','',file.destinationDBlock)
      # add retry num
                        if file.dataset.endswith('/') or job.prodSourceLabel == 'panda':
                            oldOutDsName = file.destinationDBlock
                            retryDsPatt = '_r'
                            if reproduceFiles != []:
                                retryDsPatt = '_rp'
                            retryMatch = re.search(retryDsPatt+'(\d+)$',file.destinationDBlock)
                            if retryMatch == None:
                                file.destinationDBlock += (retryDsPatt+'1')
                            else:
                                tmpDestinationDBlock = re.sub(retryDsPatt+'(\d+)$','',file.destinationDBlock)
                                file.destinationDBlock = tmpDestinationDBlock + retryDsPatt + '%d' % (1+int(retryMatch.group(1)))
                            if job.processingType == 'usermerge':
                                job.jobParameters = job.jobParameters.replace(' %s ' % oldOutDsName,
                                                                              ' %s ' % file.destinationDBlock)
          # use new dataset name for buildXYZ
                            if job.prodSourceLabel == 'panda':
                                if file.lfn.endswith('.lib.tgz'):
                                    # get new libDS and lib.tgz names
                                    oldLibDS  = file.dataset
                                    file.dataset = file.destinationDBlock
                                    newLibDS = file.dataset
                                    file.lfn = re.sub(oldLibDS,newLibDS,file.lfn)
                                    newLibTgz = file.lfn
                                else:
                                    file.dataset = file.destinationDBlock                                    
                        # add attempt nr
                        oldName  = file.lfn
                        if job.prodSourceLabel == 'panda' and file.lfn.endswith('.lib.tgz'):
                            continue
                        else:
                            # append attempt number at the tail 
                            file.lfn = re.sub("\.\d+$","",file.lfn)
                            file.lfn = "%s.%d" % (file.lfn,job.attemptNr)
                        newName  = file.lfn
                        # modify jobParameters
                        job.jobParameters = re.sub("'%s'" % oldName ,"'%s'" % newName,
                                                   job.jobParameters)
                        # look for output in trf
                        oldGenelicName = re.sub('\.\d+$','',oldName)
                        match = re.search(oldGenelicName+'(\.\d+)*(%20|")',job.jobParameters)
                        if match != None:
                            job.jobParameters = job.jobParameters.replace(match.group(0),newName+match.group(2))
                # change lib.tgz name
                if retryBuild and newLibDS != None:
                    job.jobParameters = re.sub(oldLibDS,newLibDS,job.jobParameters)
                    # change destinationDBlock
                    if job.prodSourceLabel == 'panda':
                        job.destinationDBlock = newLibDS
                # all files are used by others
                if numUsedFiles == 0 and skipInputList != []:
                    continue
                # remove skipped files
                strSkipped = ''
                for tmpFile in skipInputList:
                    strSkipped += '%s,' % tmpFile.lfn
                    job.Files.remove(tmpFile)
                strSkipped = strSkipped[:-1]
                # modify jobpar
                if strSkipped != '':
                    optionToSkipFiles = '--skipInputByRetry'
                    if not optionToSkipFiles in job.jobParameters:
                        # just append
                        job.jobParameters += "%s=%s " % (optionToSkipFiles,strSkipped)
                    else:
                        # extract already skipped files
                        tmpMatch = re.search("(%s=[^ ]+)",job.jobParameters)
                        if tmpMatch == None:
                            tmpLog.error("Failed to extract arg of %s for PandaID=%s" \
                                         % (optionToSkipFiles,job.PandaID))
                            return
                        # replace
                        job.jobParameters = re.sub(tmpMatch.group(1),"%s,%s" % (tmpMatch.group(1),optionToSkipFiles),
                                                   job.jobParameters)
                if self.verbose:
                    tmpLog.debug(job.jobParameters)
                # append
                retryJobs.append(job)
            # info on rebrokeage    
            if rebroMap != {}:
                for tmpRebKey,tmpRebNumJobs in rebroMap.iteritems():
                    tmpRebSetID,tmpRebJobID = tmpRebKey
                    tmpLog.info('Skip %s jobs since JobID=%s JobsetID=%s already reassigned them to another site' % \
                                (tmpRebNumJobs,tmpRebJobID,tmpRebSetID))
                if retryJobs == []:
                    tmpLog.info("No more jobs to be retried for JobID=%s" % JobID)
                    if isJobset:
                        continue
                    else:
                        return
            # all input files were or are being used by other jobs
            if retryJobs == []:
                tmpLog.info('All input files were or are being used by other jobs for the same output. No jobs to be retried. If you need to ignore duplication check (e.g., using the same EVNT file for multiple simulation subjobs), set ignoreDuplication=True. i.e. retry(123,ignoreDuplication=True)')
                if isJobset:
                    continue
                else:
                    return
            # check voms role
            if not retryJobs[0].workingGroup in ['NULL',None,'']:
                # VOMS role was used 
                if not "--workingGroup" in job.metadata:
                    # extract voms roles from metadata
                    match =  re.search("--voms( |=)[ \"]*([^ \"]+)",job.metadata)
                    if match != None:
                        vomsRoles = match.group(2)
                    else:
                        vomsRoles = "atlas:/atlas/%s/Role=production" % retryJobs[0].workingGroup
                # regenerate proxy with VOMS roles
                try:
                    tmpLog.info("Checking proxy role to resubmit %s jobs" % retryJobs[0].workingGroup)
                    self.gridPassPhrase,self.vomsFQAN = PsubUtils.checkGridProxy(
                            self.gridPassPhrase,
                            False,
                            self.verbose,vomsRoles,
                            useCache=True)
                except:
                    tmpLog.error("Failed to generate a proxy with %s" % vomsRoles)
                    return
            # check runtime env for new site submission
            if (newSite or newOpts != {}):
                if retryJobs[0].processingType == 'pathena' or '--useAthenaPackages' in retryJobs[0].metadata:
                    from pandatools import AthenaUtils
                    stA,retA = AthenaUtils.getAthenaVer()
                    if not stA:
                        tmpLog.error("Failed to get Athena rel/cache version in current runtime env")
                        return
                    athenaVer = retA['athenaVer']
                    cacheVer  = retA['cacheVer']
                    nightVer  = retA['nightVer']
                    wrongSetup = False
                    if retryJobs[0].AtlasRelease != 'Atlas-%s' % athenaVer:
                        wrongSetup = True
                        errMsg =  "Current Athena version Atlas-%s is inconsitent with the previous submission %s. " % (athenaVer,retryJobs[0].AtlasRelease)
                    elif retryJobs[0].homepackage != 'AnalysisTransforms'+cacheVer+nightVer:
                        wrongSetup = True                        
                        errMsg =  "Current cache version %s is inconsitent with the previous submission. " % cacheVer.replace('-','').replace('_','-')
                    if wrongSetup:    
                        errMsg += 'You need to have the same runtime env as before since all job spec need to be re-created to send jobs to a new site. '
                        errMsg += 'Please setup Athena correctly and restart pbook'                        
                        tmpLog.error(errMsg)
                        return
            # test mode
            if noSubmit:
                continue
            # invoke pathena/prun to send job to new site
            if (newSite or newOpts != {}) and retryJobs[0].processingType != 'usermerge':
                # set parent jobID and jobsetID
                newOpts['provenanceID'] = retryJobs[0].jobExecutionID
                newOpts['panda_parentJobsetID'] = retryJobs[0].sourceSite
                tmpLog.info("Constructing job spec again to be sent to another site ...")
                comStat= PsubUtils.execWithModifiedParams(retryJobs,newOpts,self.verbose,newSite)
                if comStat == 0:
                    # update database
                    time.sleep(2)
                    self.sync()
                else:
                    tmpLog.error("Failed to submit jobs to Panda server")                
                return
            # register datasets
            tmpOutDsLocation = Client.PandaSites[retryJobs[-1].computingSite]['ddm']
            addedDataset = []
            shadowDSname = None
            for tmpFile in retryJobs[-1].Files:
                if tmpFile.type in ['output','log'] and tmpFile.dataset.endswith('/'):
                    # add shadow
                    """
                    removed shadow
                    if shadowDSname == None and tmpFile.type == 'log':
                        shadowDSname = "%s%s" % (tmpFile.destinationDBlock,Client.suffixShadow)
                        Client.addDataset(shadowDSname,self.verbose)
                    """    
                    # add datasets    
                    if not tmpFile.destinationDBlock in addedDataset:
                        # create dataset
                        Client.addDataset(
                                tmpFile.destinationDBlock,
                                self.verbose,
                                location=tmpOutDsLocation,
                                dsCheck=False)
                        # add to container
                        Client.addDatasetsToContainer(
                                tmpFile.dataset,
                                [tmpFile.destinationDBlock],
                                self.verbose)
                        # append
                        addedDataset.append(tmpFile.destinationDBlock)
            # register libDS
            if retryBuild and newLibDS != None:
                Client.addDataset(
                        newLibDS,
                        self.verbose,
                        location=tmpOutDsLocation,
                        dsCheck=False)
            # submit
            tmpLog.info("Submitting job ...")            
            status,out = Client.submitJobs(retryJobs,verbose=self.verbose)
            if out == None or status != 0:
                tmpLog.error(status)
                tmpLog.error(out)
                tmpLog.error("Failed to submit jobs to Panda server")
                return
            # update database
            pandaIDstatus = {}
            newJobID = None
            for items in out:
                # get newJobID
                if newJobID == None:
                    newJobID = items[1]
                # check PandaID
                PandaID = items[0]
                if PandaID == 'NULL':
                    tmpLog.error("Panda server returned wrong IDs. It may have a temporary problem")
                    return
                # set newJobsetID
                if newJobsetID in [None,-1]:
                    newJobsetID = items[2]['jobsetID']
                # dummy statuso
                pandaIDstatus[PandaID] = ('defined','NULL')
            # set retry ID
            if not unsetRetryID:
                localJob.retryID = newJobID
                if not newJobsetID in [None,-1,'NULL']:
                    localJob.retryJobsetID = newJobsetID
                try:
                    PdbUtils.updateJobDB(localJob,self.verbose)
                except:
                    tmpLog.error("Failed to set retryID for JobID=%s" % JobID)
                    return
            # set new paramers
            newLocalJob = PdbUtils.convertPtoD(retryJobs,pandaIDstatus)
            newLocalJob.JobID = newJobID
            if not newJobsetID in [None,-1,'NULL']:
                newLocalJob.groupID = newJobsetID
            newLocalJob.creationTime = datetime.datetime.utcnow()
            # insert to DB
            try:
                PdbUtils.insertJobDB(newLocalJob,self.verbose)
            except:
                tmpLog.error("Failed to insert JobID=%s to local repository" % newJobID)
                return
            # write new jobdefID
            PsubUtils.writeJobDefID(newJobID)
            # done
            tmpMsg = 'Done. New JobID=%s' % newJobID
            if not newJobsetID in [None,-1,'NULL']:
                tmpMsg += " JobsetID=%s" % newJobsetID
            tmpLog.info(tmpMsg)


    # convert taskID to jobsetID
    def convertTaskToJobID(self,taskID):
        if taskID in self.jobsetTaskMap:
            return self.jobsetTaskMap[taskID]
        return taskID
