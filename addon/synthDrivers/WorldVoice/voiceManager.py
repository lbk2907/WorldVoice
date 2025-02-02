from collections import OrderedDict, defaultdict

import threading

import config
import languageHandler
from logHandler import log
from synthDriverHandler import VoiceInfo

from .taskManager import TaskManager
from .voice.VEVoice import VEVoice
from .voice.Sapi5Voice import Sapi5Voice
from .voice.AisoundVoice import AisoundVoice
from .voice.OneCoreVoice import OneCoreVoice
from .voice.RHVoice import RHVoice


def joinObjectArray(srcArr1, srcArr2, key):
	mergeArr = []
	for srcObj2 in srcArr2:
		def exist(existObj):
			mergeObj = {}
			if len(existObj) > 0:
				mergeObj = {**existObj[0], **srcObj2}
				mergeArr.append(mergeObj)
		exist(
			list(filter(lambda srcObj1: srcObj1[key] == srcObj2[key], srcArr1))
		)

	return mergeArr


def groupByField(arrSrc, field, applyKey, applyValue):
	temp = {}
	for item in arrSrc:
		key = applyKey(item[field])
		temp[key] = temp[key] if key in temp else []
		temp[key].append(applyValue(item))

	return temp


class VoiceManager(object):
	voice_class = {
		"VE": VEVoice,
		"SAPI5": Sapi5Voice,
		"aisound": AisoundVoice,
		"OneCore": OneCoreVoice,
		"RH": RHVoice,
	}

	@classmethod
	def ready(cls):
		result = False
		for item in cls.voice_class.values():
			result |= item.ready()
		return result

	def __init__(self):
		self.lock = threading.Lock()

		active_engine = []
		for item in self.voice_class.values():
			if item.ready():
				try:
					item.engineOn(self.lock)
					active_engine.append(item)
				except BaseException:
					pass

		self.table = []
		for item in active_engine:
			self.table.extend(item.voices())

		self.taskManager = TaskManager(lock=self.lock, table=self.table)

		self._setVoiceDatas()
		self.engine = 'ALL'

		self._instanceCache = {}
		self.waitfactor = 0

		self._defaultVoiceInstance = self.getVoiceInstance(self.table[0]["name"])
		self._defaultVoiceInstance.loadParameter()
		log.debug("Created voiceManager instance. Default voice is %s", self._defaultVoiceInstance.name)

	def terminate(self):
		for voiceName, instance in self._instanceCache.items():
			instance.commit()
			instance.close()

		for item in self.voice_class.values():
			try:
				item.engineOff()
			except BaseException:
				pass

		self.taskManager = None

	@property
	def defaultVoiceInstance(self):
		return self._defaultVoiceInstance

	@property
	def defaultVoiceName(self):
		return self._defaultVoiceInstance.name

	@defaultVoiceName.setter
	def defaultVoiceName(self, name):
		if name not in self._voiceInfos:
			log.debugWarning("Voice not available, using default voice.")
			return
		self._defaultVoiceInstance = self.getVoiceInstance(name)

	@property
	def waitfactor(self):
		return self._waitfactor

	@waitfactor.setter
	def waitfactor(self, value):
		self._waitfactor = value
		for voiceName, instance in self._instanceCache.items():
			instance.waitfactor = value
			instance.commit()

	def getVoiceInstance(self, voiceName):
		try:
			instance = self._instanceCache[voiceName]
		except KeyError:
			instance = self._createVoiceInstance(voiceName)
		return instance

	def _createVoiceInstance(self, voiceName):
		item = list(filter(lambda item: item["name"] == voiceName, self.table))[0]
		voiceInstance = self.voice_class[item["engine"]](id=item["id"], name=item["name"], language=item["language"], taskManager=self.taskManager)
		voiceInstance.loadParameter()
		voiceInstance.waitfactor = self.waitfactor

		self._instanceCache[voiceInstance.name] = voiceInstance
		return self._instanceCache[voiceInstance.name]

	def onVoiceParameterConsistent(self, baseInstance):
		for voiceName, instance in self._instanceCache.items():
			instance.rate = baseInstance.rate
			instance.pitch = baseInstance.pitch
			instance.volume = baseInstance.volume
			instance.commit()

	def onKeepEngineConsistent(self):
		temp = defaultdict(lambda: {})
		for key, value in config.conf["WorldVoice"]["speechRole"].items():
			if isinstance(value, config.AggregatedSection):
				try:
					temp[key]['voice'] = config.conf["WorldVoice"]["speechRole"][key]["voice"]
				except KeyError:
					pass
			else:
				temp[key] = config.conf["WorldVoice"]["speechRole"][key]

		for localelo, data in config.conf["WorldVoice"]['speechRole'].items():
			if isinstance(data, config.AggregatedSection):
				if (localelo not in self.localeToVoicesMapEngineFilter) or ('voice' in data and data['voice'] not in self.localeToVoicesMapEngineFilter[localelo]):
					try:
						del temp[localelo]
					except BaseException:
						pass
					try:
						log.info("locale {locale} voice {voice} not available on {engine} engine".format(
							locale=localelo,
							voice=data['voice'],
							engine=self.engine,
						))
					except BaseException:
						pass

		config.conf["WorldVoice"]["speechRole"] = temp
		if self.taskManager:
			self.taskManager.reset_SAPI5()

	def reload(self):
		for voiceName, instance in self._instanceCache.items():
			instance.loadParameter()

	def cancel(self):
		self._defaultVoiceInstance.stop()
		if self.taskManager:
			self.taskManager.cancel()
			if True or not self.taskManager.block:
				for voiceName, instance in self._instanceCache.items():
					instance.stop()

	def _setVoiceDatas(self):
		self.table = []
		for item in self.voice_class.values():
			self.table.extend(item.voices())
		self.table = sorted(self.table, key=lambda item: (item['engine'], item['language'], item['name']))

		self._localesToVoices = {
			**groupByField(self.table, 'locale', lambda i: i, lambda i: i['name']),
			# For locales with no country (i.g. "en") use all voices from all sub-locales
			**groupByField(self.table, 'locale', lambda i: i.split('_')[0], lambda i: i['name']),
		}

		self._voicesToEngines = {}
		for item in self.table:
			self._voicesToEngines[item["name"]] = item["engine"]

		voiceInfos = []
		for item in self.table:
			voiceInfos.append(VoiceInfo(item["name"], item["description"], item["language"]))

		# Kepp a list with existing voices in VoiceInfo objects.
		self._voiceInfos = OrderedDict([(v.id, v) for v in voiceInfos])

	def _setVoiceDatasEngineFilter(self):
		self.tableEngineFilter = []
		for item in self.voice_class.values():
			self.tableEngineFilter.extend(item.voices())
		self.tableEngineFilter = sorted(self.tableEngineFilter, key=lambda item: (item['engine'], item['language'], item['name']))
		self.tableEngineFilter = list(filter(lambda item: self.engine == 'ALL' or item['engine'] == self.engine, self.tableEngineFilter))

		self._localesToVoicesEngineFilter = {
			**groupByField(self.tableEngineFilter, 'locale', lambda i: i, lambda i: i['name']),
			# For locales with no country (i.g. "en") use all voices from all sub-locales
			**groupByField(self.tableEngineFilter, 'locale', lambda i: i.split('_')[0], lambda i: i['name']),
		}

		self._voicesToEnginesEngineFilter = {}
		for item in self.tableEngineFilter:
			self._voicesToEnginesEngineFilter[item["name"]] = item["engine"]

		voiceInfos = []
		for item in self.tableEngineFilter:
			voiceInfos.append(VoiceInfo(item["name"], item["description"], item["language"]))

		# Kepp a list with existing voices in VoiceInfo objects.
		self._voiceInfosEngineFilter = OrderedDict([(v.id, v) for v in voiceInfos])

	@property
	def engine(self):
		return self._engine

	@engine.setter
	def engine(self, value):
		if value not in ["ALL"] + list(self.voice_class.keys()):
			raise ValueError("engine setted is not valid")
		self._engine = value
		self._setVoiceDatasEngineFilter()

	@property
	def voiceInfos(self):
		return self._voiceInfos

	@property
	def languages(self):
		return sorted([l for l in self._localesToVoices if len(self._localesToVoices[l]) > 0])

	@property
	def localeToVoicesMap(self):
		return self._localesToVoices.copy()

	@property
	def localesToNamesMap(self):
		return {item: self._getLocaleReadableName(item) for item in self._localesToVoices}

	@property
	def languagesEngineFilter(self):
		return sorted([l for l in self._localesToVoicesEngineFilter if len(self._localesToVoicesEngineFilter[l]) > 0])

	@property
	def localeToVoicesMapEngineFilter(self):
		return self._localesToVoicesEngineFilter.copy()

	@property
	def localesToNamesMapEngineFilter(self):
		return {locale: self._getLocaleReadableName(locale) for locale in self._localesToVoicesEngineFilter}

	def getVoiceNameForLanguage(self, language):
		configured = self._getConfiguredVoiceNameForLanguage(language)
		if configured is not None and configured in self.voiceInfos:
			return configured
		return self.defaultVoiceName

		# deprecation
		voices = self._localesToVoicesEngineFilter.get(language, None)
		if voices is None:
			if '_' in language:
				voices = self._localesToVoicesEngineFilter.get(language.split('_')[0], None)
		if voices is None:
			return None
		voice = self.defaultVoiceName if self.defaultVoiceName in voices else voices[0]
		return voice

	def getVoiceInstanceForLanguage(self, language):
		voiceName = self.getVoiceNameForLanguage(language)
		if voiceName:
			return self.getVoiceInstance(voiceName)
		return None

	def _getConfiguredVoiceNameForLanguage(self, language):
		voice = None
		if language in config.conf["WorldVoice"]['speechRole']:
			try:
				voice = config.conf["WorldVoice"]['speechRole'][language]['voice']
			except BaseException:
				pass
		if not voice:
			if '_' not in language:
				return voice
			language = language.split('_')[0]
			if language in config.conf["WorldVoice"]['speechRole']:
				try:
					voice = config.conf["WorldVoice"]['speechRole'][language]['voice']
				except BaseException:
					pass
		return voice

	def _getLocaleReadableName(self, locale_):
		description = languageHandler.getLanguageDescription(locale_)
		return "%s - %s" % (description, locale_) if description else locale_
