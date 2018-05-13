#!/usr/bin/env pythoni
from ast import literal_eval
import collections
import gspread
import logging
import json
from oauth2client.service_account import ServiceAccountCredentials
#import RPi.GPIO as GPIO
import random
import time
import subprocess
from datetime import datetime
import sys
import os
import multiprocessing 
import Queue

CONFIG_FILE = 'config.json'
CREDENTIALS_FILE = 'self-video-zach-208096653289.json'
LOCAL_SCHEDULE_CACHE_FILE = 'schedule_cache.json'  # Used as fallback schedule if google sheet won't validate
LOCAL_GSHEET_PAGE_CACHE = 'gsheet_page_cache.json' # A Copy of the state of the google sheets event page 
SCOPE = ['https://spreadsheets.google.com/feeds',
         'https://www.googleapis.com/auth/drive']

# Define how the Google sheet is set up
SHEET_NAME = 'speakers-list'
SHEET_COL_HDG_EVENT_ID = 'EventID'
SHEET_COL_HDG_EVENT_ROOM = 'Room'
SHEET_COL_HDG_EVENT_DATE = 'Date'
SHEET_COL_HDG_EVENT_TIME = 'startTime'

Event = collections.namedtuple('Event', 'id room date time')

class FeedbackCollector:
	'''FeedbackCollector handles collection of GPIO events and sending them to queue.

	Args: 
		queue: Multiprocessing queue where output is placed.

	Attributes:
		config (dict): Configuration parameters loaded from external config file.
		credentials: Google account credentials as created from external credentials file.
		currentEvent: Current event id being logged.
		gc: Google connection created using credentials
		gsheet: The Google Sheet being used
		worksheet: The page (sheet/tab within the gsheet) being used.

	'''

	@staticmethod
	def getConfig(filename):
		'''Reads the local config file (json format) from file system

		Args:
			filename: Filename of external json file with configuration.

		Returns:
			dict representation of the json config file.	
		'''
		with open(filename) as f:
			return json.load(f)

	def buildSchedule(self, logger):
		'''Builds/Updates the Event Schedule for this particular device based on the room from
		the device config.

		Args:
			none

		Returns:
			(list of Event tuples) Current room schedule
		'''
		gSchedule = self.gsheet.worksheet(self.config['schedule_sheet']).get_all_records()

		# save a local copy of the worksheet to filesystem.
		# Mainly, just so somebody has all the event information locally if needed after the event.
		with open(LOCAL_GSHEET_PAGE_CACHE, 'w') as f_out:
			json.dump(gSchedule, f_out)

		schedule = []
		for row in gSchedule:
			event = Event(id=row[SHEET_COL_HDG_EVENT_ID], 
				room=row[SHEET_COL_HDG_EVENT_ROOM], 
				date=row[SHEET_COL_HDG_EVENT_DATE], 
				time=row[SHEET_COL_HDG_EVENT_TIME])
			if( event.room == self.config['room_id']):
				# Found event that matches with this device's configuration, add to schedule
				logger.debug('''Adding event to schedule: id {0} in {1} on {2} @ {3}'''
					.format(event.id, event.room, event.date, event.time))
				schedule.append(event)

		return schedule

	def validateSchedule(self, logger):
		'''Sanity checks on room schedule / list of events

		A list of room events is expected to be non-zero and no 2 events should
		exist for same date and time.

		Note: Validation fails/passes silently. Only logs the details.

		Note 2: In the event of failed validation of Google Sheet schedule, 
		will attempt to load schedule from schedule_cache.json in local filesystem.

		Args:
			s: (list) Room scheduled event list
			logger: (logger) The logger to write results to
		'''
		cnt = len(self.roomSchedule)
		loadFromCache = False
		if(cnt):
			logger.debug('Validating {0} events in room\'s schedule.'.format(cnt))
			for i in xrange(0, cnt-1):
				e1 = self.roomSchedule[i]  # The event being validated in the schedule
				for j in xrange(i+1, cnt):
					e2 = self.roomSchedule[j] # Some other event in the schedule
					
					if e1.date == e2.date and e1.time == e2.time:
						logger.degug('''Event schedule FAILS VAILIDATION. Duplicate 
							date or time found in schedule.''')
						loadFromCache = True
		else:
			logger.debug('Event schedule is empty.')
			loadFromCache = True

		if not loadFromCache:	
			logger.info('Successfully validated {0} events for roomID: \'{1}\' schedule.'
				.format(cnt, self.config['room_id']))

			# Had successful validation, update any cached copy with this one.	
			logger.debug('Caching copy of validated schedule to local filesystem.')
			schedule = {}
			schedule['configuration'] = self.config
			event_ids, rooms, dates, times = [], [], [], []
			for i in range(len(self.roomSchedule)):
				event_ids.append(self.roomSchedule[i].id)
				rooms.append(self.roomSchedule[i].room)
				dates.append(self.roomSchedule[i].date)
				times.append(self.roomSchedule[i].time)
			events = [{SHEET_COL_HDG_EVENT_ID: i, SHEET_COL_HDG_EVENT_ROOM: r,
				SHEET_COL_HDG_EVENT_DATE: d, SHEET_COL_HDG_EVENT_TIME:t} for i,r,d,t in 
				zip(event_ids, rooms, dates, times)]
			schedule['events'] = json.dumps(literal_eval(str(events)))
			with open(LOCAL_SCHEDULE_CACHE_FILE, 'w') as f_out:
				json.dump(schedule, f_out)
			return
		
		# If here, need to try to read cached copy of schedule from file system
		# Assumes that any schedule that got saved previously must have passed
		# validation to get there in the first place.
		logger.debug("Updating roomSchedule with cached schedule from file system.")	
		logger.debug("(local schedule_cache.json assumed to be good.)")
		with open(LOCAL_SCHEDULE_CACHE_FILE, 'r') as f_in:
				self.roomSchedule = json.load(f_in)
				# TODO: This could still stand additional robustness, but think it 
				# is good enough for now.

	def collectFeedback(self):
		'''Perform the feedback collection activity.

		Note: Once started, loops infinitely doing the following:
		 * Based on current time, decide what event we are logging
		 * Listen for GPIO / Button input
		 * When button press is detected, write a new vote to the queue.
		'''
		self.logger.info('FeedbackCollector.collectFeedback() loop started.')
		start_datetime = datetime.now()
		if self.config['simulate_voting'] == 'True':
			# initialize simulated voting time
			sim_year = int(start_datetime.year)
			sim_month = int(self.config['simulate_date'][0:2])
			sim_day = int(self.config['simulate_date'][3:5])
			sim_hour = int(self.config['simulate_time_start'][0:2])
			sim_min = int(self.config['simulate_time_start'][3:5])
			sim_datetime = datetime(sim_year, sim_month, sim_day, sim_hour, sim_min)
			self.logger.info('SIMULATED FEEDBACK option running (see config file)')
			self.logger.info('year: {0} month: {1} day: {2} hour: {3} min: {4}'.format(sim_year,
				sim_month, sim_day, sim_hour, sim_min))
			self.logger.info('Simulated start time set to: {0}'.format(sim_datetime))

			vote_options = ['Positive', 'Negative', 'Neutral']

		while True:
			cur_datetime = datetime.now()
			if self.config['simulate_voting'] == 'True':
				# Simulate Feedback as crude testing
				delta = (cur_datetime - start_datetime)
				if delta.seconds >= 3:
					# Simulator will make a random vote every 3 seconds
					# Update start_datetime to current time
					start_datetime = cur_datetime
					vote = random.choice(vote_options)
					record = {}
					record[SHEET_COL_HDG_EVENT_ROOM] = self.config['room_id']
					record['Timestamp'] = str(cur_datetime)
					record['Vote'] = vote

					# Add the record to the multiprocessing queue for the feedback writer
					self.logger.info("SIMULATION: collected feedback record: \n{0}".format(record))
					self.queue.put(record)
					self.logger.info("SIMULATION: wrote feedback record to queue.")

				



			# Lookup what event in schedule

			# Collect feedback here


	def __init__(self, queue, logger):
		'''Instantiate new FeedbackCollector Object

		Args:
			queue: Multiprocessing queue object to be loaded with collected feedback.
			logger: logging object from main - probably not best way to do this, but it works.
		'''
		self.config = self.getConfig(CONFIG_FILE)
		self.credentials = ServiceAccountCredentials.from_json_keyfile_name(CREDENTIALS_FILE, SCOPE)
		self.gc = gspread.authorize(self.credentials)
		self.gsheet = self.gc.open(SHEET_NAME)
		self.worksheet = self.gsheet.worksheet(self.config['room_id'])
		self.queue = queue
		self.logger = logger

		self.roomSchedule = self.buildSchedule(logger)

		# Results of schedule validation will be written to log only
		self.validateSchedule(logger)
		
		currenttime = datetime.now().strftime('%m_%d_%H_%M_%S')
		self.voteLogFile = "%s_feedback.log" % str(currenttime)

	def __repr__(self):
		# Overly verbose __repr__ because we may be headless and relying on log for debug
		myRepr = "FeatureCollector Object \n"
		myRepr += " .config = {0}\n".format(self.config)
		myRepr += " .gsheet = {0}\n".format(self.gsheet)
		myRepr += " .worksheet = {0}\n".format(self.worksheet)
		myRepr += " .voteLogFile = {0}\n".format(self.voteLogFile)
		myRepr += " .roomSchedule = {0}\n".format(self.roomSchedule)

		return myRepr

class FeedbackWriter:
	'''FeebackWriter object is responsible for updating vote tallies and writing to 
	local file and Google Sheets.
	'''

	def writeFeedback(self):
		self.logger.info('FeedbackWriter.writeFeedback() loop started')
		while True:
			feedback_list = []
			time.sleep(5) # Do processing once every 5 seconds - No need to go wide open.

			while self.queue.qsize() != 0:
				feedback_list.append(self.queue.get())
			
			self.logger.info('FeedbackWriter read feedback from queue:\n{0}'.format(feedback_list))



	def __init__(self, queue, logger):
		'''Instantiate new FeedbackWriter Object

		Args:
			queue: Multiprocessing queue object that we need to write the feedback from.
			logger: logging object from main - probably a better way to do this, but it works.
		'''
		self.queue = queue
		self.logger = logger

def start(fc, logger):
	logger.info('Entered start() function')
	stop_writing  = updater()
	#GPIO.setmode(GPIO.BCM)

	#GPIO.setup(18, GPIO.IN, pull_up_down=GPIO.PUD_UP)
	#GPIO.setup(13, GPIO.IN, pull_up_down=GPIO.PUD_UP)
	#GPIO.setup(6, GPIO.IN, pull_up_down=GPIO.PUD_UP)
	try: input()
	except KeyboardInterrupt: sys.exit()
	finally:
		stop_writing.set()
		#GPIO.output(6,GPIO.LOW)
		#GPIO.output(13,GPIO.LOW)
		#GPIO.output(18,GPIO.LOW)
	#vote = "pos"
	#updater(vote)	

def googlesheetlookup():
	currenttime = datetime.now().strftime('%m-%d-%H:%M')
	cell = worksheet.find(currenttime)

	talkID = worksheet.acell("""A""" + str(cell.row) + """ """).value
	print "Talk ID = ", talkID
	updater(talkID)

def input():
	while True:
		#input_state18 = GPIO.input(18)
		#input_state13 = GPIO.input(13)
		#input_state6 = GPIO.input(6)
		input_state18 = True;
		input_state13 = False;
		input_state6 = False;
		if input_state18 == False:
			vote = "pos"
		if input_state13 == False:
			vote = "neg"
		if input_state6 == False:
			vote = "neutral"
		queue.put(vote)
		time.sleep(1)#seconds
	
def updater():
	def update(stop):
		while not stop.is_set():
			try:
				for _ in range(0, queue.qsize()):
					vote = queue.get_nowait()
					if vote is "pos":
							updatepos()
					if vote is "neg":
							updateneg()
					if vote is "neutral":
							updateneutral()
					time.sleep(1) # seconds
			except Queue.Empty: pass
			except KeyboardInterrupt: pass
	stop = multiprocessing.Event()
	multiprocessing.Process(target=update, args=[stop]).start()
	return stop


def updatepos(talkID):
	cell = worksheet.find(talkID)
	value = worksheet.acell("""I""" + str(cell.row) + """ """).value
	newvalue = int(value) + 1
	worksheet.update_acell("""I""" + str(cell.row) + """ """, """ """ + str(newvalue) + """ """)

def updateneg():

	cell = worksheet.find(talkID)
	value = worksheet.acell("""G""" + str(cell.row) + """ """).value
	newvalue = int(value) + 1
	worksheet.update_acell("""G""" + str(cell.row) + """ """, """ """ + str(newvalue) + """ """)
	
def updateneutral():

	cell = worksheet.find(talkID)
	value = worksheet.acell("""H""" + str(cell.row) + """ """).value
	newvalue = int(value) + 1
	worksheet.update_acell("""H""" + str(cell.row) + """ """, """ """ + str(newvalue) + """ """)

if __name__ == "__main__":
	# Set up for a file log, since this will be headless
	logging.basicConfig(level=logging.DEBUG,
		format='%(asctime)s %(name)-12s %(levelname)-8s %(message)s',
        datefmt='%m-%d %H:%M',
        filename='debug.log',
        filemode='w')
	logger = logging.getLogger('main')
	logger.info('START FeedbackCollector script')

	# Set up Multiprocessing queue to share
	queue = multiprocessing.Queue()

	# Instantiate our collector
	collector = FeedbackCollector(queue, logger)
	logger.info('FeedbackCollector object instantiated.')
	logger.debug('FeedbackCollector: %s' % str(collector))

	#Instantiate our writer
	writer = FeedbackWriter(queue, logger)
	logger.info('FeedbackWriter object instantiated.')

	collectorProcess = multiprocessing.Process(target=collector.collectFeedback)
	collectorProcess.start()
	writerProcess = multiprocessing.Process(target=writer.writeFeedback)
	writerProcess.start()


	collectorProcess.join()
	writerProcess.join()

