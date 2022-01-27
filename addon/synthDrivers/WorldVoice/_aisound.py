#_aisound.py
#A part of NVDA AiSound 5 Synthesizer Add-On

import os
import queue
import threading

import audioDucking
import globalVars
import NVDAHelper
from ctypes import *
from logHandler import log
from synthDriverHandler import synthIndexReached,synthDoneSpeaking

workspaceAisound_path = os.path.join(globalVars.appArgs.configPath, "WorldVoice-workspace", "aisound")
lastSpeakInstance = None
synthRef = None

aisound_callback_t=CFUNCTYPE(None,c_int,c_void_p)
SPEECH_BEGIN=0
SPEECH_END=1

class FunctionHooker(object):

	def __init__(
		self,
		targetDll: str,
		importDll: str,
		funcName: str,
		newFunction # result of ctypes.WINFUNCTYPE
	):
		# dllImportTableHooks_hookSingle expects byte strings.
		try:
			self._hook=NVDAHelper.localLib.dllImportTableHooks_hookSingle(
				targetDll.encode("mbcs"),
				importDll.encode("mbcs"),
				funcName.encode("mbcs"),
				newFunction
			)
		except UnicodeEncodeError:
			log.error("Error encoding FunctionHooker input parameters", exc_info=True)
			self._hook = None
		if self._hook:
			log.debug(f"Hooked {funcName}")
		else:
			log.error(f"Could not hook {funcName}")
			raise RuntimeError(f"Could not hook {funcName}")

	def __del__(self):
		if self._hook:
			NVDAHelper.localLib.dllImportTableHooks_unhookSingle(self._hook)

_duckersByHandle={}

@WINFUNCTYPE(windll.winmm.waveOutOpen.restype,*windll.winmm.waveOutOpen.argtypes,use_errno=False,use_last_error=False)
def waveOutOpen(pWaveOutHandle,deviceID,wfx,callback,callbackInstance,flags):
	try:
		res=windll.winmm.waveOutOpen(pWaveOutHandle,deviceID,wfx,callback,callbackInstance,flags) or 0
	except WindowsError as e:
		res=e.winerror
	if res==0 and pWaveOutHandle:
		h=pWaveOutHandle.contents.value
		d=audioDucking.AudioDucker()
		d.enable()
		_duckersByHandle[h]=d
	return res

@WINFUNCTYPE(c_long,c_long)
def waveOutClose(waveOutHandle):
	try:
		res=windll.winmm.waveOutClose(waveOutHandle) or 0
	except WindowsError as e:
		res=e.winerror
	if res==0 and waveOutHandle:
		_duckersByHandle.pop(waveOutHandle,None)
	return res

_waveOutHooks=[]
def ensureWaveOutHooks(dllPath):
	if not _waveOutHooks and audioDucking.isAudioDuckingSupported():
		_waveOutHooks.append(FunctionHooker(dllPath,"WINMM.dll","waveOutOpen",waveOutOpen))
		_waveOutHooks.append(FunctionHooker(dllPath,"WINMM.dll","waveOutClose",waveOutClose))


@aisound_callback_t
def callback(type,cbData):
	global lastSpeakInstance, synthRef
	global aisoundQueue, voiceLock
	if type==SPEECH_BEGIN:
		if cbData==None:
			lastSpeakInstance.lastIndex=0
		else:
			lastSpeakInstance.lastIndex=cbData
			synthIndexReached.notify(synth=synthRef(),index=lastSpeakInstance.lastIndex)
	elif type==SPEECH_END:
		lastSpeakInstance.isPlaying=False
		synthDoneSpeaking.notify(synth=synthRef())

		if aisoundQueue:
			try:
				aisoundQueue.task_done()
			except:
				pass
		if voiceLock:
			try:
				voiceLock.release()
			except RuntimeError:
				pass


class Aisound(object):
	def __init__(self):
		self.wrapperDLL = None
		self.isPlaying = False
		self.lastIndex = None

		if self.wrapperDLL==None:
			dllPath=os.path.join(workspaceAisound_path, "aisound.dll")
			ensureWaveOutHooks(dllPath)
			self.wrapperDLL=cdll.LoadLibrary(dllPath)
			self.wrapperDLL.aisound_callback.restype=c_bool
			self.wrapperDLL.aisound_callback.argtypes=[aisound_callback_t]
			self.wrapperDLL.aisound_configure.restype=c_bool
			self.wrapperDLL.aisound_configure.argtypes=[c_char_p,c_char_p]
			self.wrapperDLL.aisound_speak.restype=c_bool
			self.wrapperDLL.aisound_speak.argtypes=[c_char_p,c_void_p]
			self.wrapperDLL.aisound_cancel.restype=c_bool
			self.wrapperDLL.aisound_pause.restype=c_bool
			self.wrapperDLL.aisound_resume.restype=c_bool
		self.wrapperDLL.aisound_initialize()
		self.wrapperDLL.aisound_callback(callback)

	def Terminate(self):
		self.wrapperDLL.aisound_terminate()

	def Configure(self, name,value):
		return self.wrapperDLL.aisound_configure(name.encode("utf-8"),value.encode("utf-8"))

	def Speak(self, text,index=None):
		global lastSpeakInstance
		if index==None:
			cbData=0
		else:
			cbData=index
		self.isPlaying=True
		lastSpeakInstance = self

		return self.wrapperDLL.aisound_speak(text.encode("utf-8"),c_void_p(cbData))

	def Cancel(self):
		self.isPlaying=False
		return self.wrapperDLL.aisound_cancel()

	def Pause(self):
		return self.wrapperDLL.aisound_pause()

	def Resume(self):
		return self.wrapperDLL.aisound_resume()

class AisoundThread(threading.Thread):
	def __init__(self, aisoundQueue):
		super().__init__()
		self.aisoundQueue = aisoundQueue
		self.setDaemon(True)
		self.start()

	def run(self):
		while True:
			voiceInstance, text, mode = self.aisoundQueue.get()
			if not voiceInstance:
				break
			try:
				voiceInstance.active()
				if mode == "speak":
					voiceInstance.aisound.Speak(text, None)
				elif mode == "speak_index":
					voiceInstance.aisound.Speak("", text)
			except Exception:
				log.error("Error running function from queue", exc_info=True)
			# self.aisoundQueue.task_done()


aisoundQueue = None
aisoundThread = None

def initialize(getSynth):
	global synthRef
	synthRef = getSynth

	global aisoundThread, aisoundQueue
	aisoundQueue = queue.Queue()
	aisoundThread = AisoundThread(
		aisoundQueue=aisoundQueue
	)

def terminate():
	global synthRef
	synthRef = None

	global aisoundThread, aisoundQueue
	if aisoundThread:
		aisoundQueue.put((None, None, None),)
		aisoundThread.join()
	del aisoundQueue
	del aisoundThread
	aisoundThread, aisoundQueue = None, None

voiceLock = None
