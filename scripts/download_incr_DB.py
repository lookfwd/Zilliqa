#!/usr/bin/env python
# Copyright (C) 2019 Zilliqa
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

import requests 
import xml.etree.ElementTree as ET
import re
import tarfile
from clint.textui import progress
import os, sys
import shutil
import time
import datetime
from concurrent.futures import ThreadPoolExecutor
from threading import Thread, Lock
import hashlib

PERSISTENCE_SNAPSHOT_NAME='incremental'
STATEDELTA_DIFF_NAME='statedelta'
BUCKET_NAME='BUCKET_NAME'
CHUNK_SIZE = 4096
EXPEC_LEN = 2
TESTNET_NAME= 'TEST_NET_NAME'
MAX_WORKER_JOBS = 50
S3_MULTIPART_CHUNK_SIZE_IN_MB = 8 

Exclude_txnBodies = True
Exclude_microBlocks = True

BASE_PATH = os.path.dirname(os.path.realpath(sys.argv[0]))
STORAGE_PATH = BASE_PATH
mutex = Lock()

def getURL():
	return "http://"+BUCKET_NAME+".s3.amazonaws.com"

def UploadLock():
	response = requests.get(getURL()+"/"+PERSISTENCE_SNAPSHOT_NAME+"/"+TESTNET_NAME+"/.lock")
	if response.status_code == 200:
		return True
	return False

def GetEntirePersistenceFromS3():
	CleanupDir(STORAGE_PATH + "/persistence")
	CreateAndChangeDir(STORAGE_PATH)
	if GetAllObjectsFromS3(getURL(),PERSISTENCE_SNAPSHOT_NAME) == 1 :
		exit(1)


def GetStateDeltaFromS3():
	CleanupCreateAndChangeDir(STORAGE_PATH+'/StateDeltaFromS3')
	GetAllObjectsFromS3(getURL(), STATEDELTA_DIFF_NAME)
	ExtractAllGzippedObjects()

def GetAllObjectsFromS3(url, folderName=""):
	MARKER = ''
	list_of_keyurls = []
	prefix = ""
	if folderName:
		prefix = folderName+"/"+TESTNET_NAME
	# Try get the entire persistence keys.
	# S3 limitation to get only max 1000 keys. so work around using marker.
	while True:
		response = requests.get(url, params={"prefix":prefix, "max-keys":1000, "marker": MARKER})
		tree = ET.fromstring(response.text)
		startInd = 5
		if(tree[startInd:] == []):
			print("Empty response")
			return 1
		print("[" + str(datetime.datetime.now()) + "] Files to be downloaded:")
		lastkey = ''
		for key in tree[startInd:]:
			key_url = key[0].text
			if (not (Exclude_txnBodies and "txBodies" in key_url) and not (Exclude_microBlocks and "microBlocks" in key_url)):
				list_of_keyurls.append(url+"/"+key_url)
				print(key_url)
			lastkey = key_url
		istruncated=tree[4].text
		if istruncated == 'true':
			MARKER=lastkey
			print(istruncated)
		else:
			break

	with ThreadPoolExecutor(max_workers=MAX_WORKER_JOBS) as pool:
		pool.map(GetPersistenceKey,list_of_keyurls)
		pool.shutdown(wait=True)
	print("[" + str(datetime.datetime.now()) + "]"+" All objects from " + url + " completed!")
	return 0

def GetPersistenceKey(key_url):
	retry_counter = 0
	while True:
		response = requests.get(key_url, stream=True)
		filename = key_url.replace(key_url[:key_url.index(TESTNET_NAME+"/")+len(TESTNET_NAME+"/")],"").strip()

		dirname = os.path.dirname(filename).strip()
		if dirname != "":
			mutex.acquire()
			if not os.path.exists(dirname):
				os.makedirs(dirname)
			mutex.release()

		with open(filename,'wb') as f:
			total_length = response.headers.get('content-length')
			total_length = int(total_length)
			md5_hash = response.headers.get('ETag')
			for chunk in progress.bar(response.iter_content(chunk_size=CHUNK_SIZE), expected_size=(total_length/CHUNK_SIZE) + 1):
				if chunk:
					f.write(chunk)
					f.flush()
			print("[" + str(datetime.datetime.now()) + "]"+" Downloaded " + filename + " successfully")
		calc_md5_hash = calculate_multipart_etag(filename, S3_MULTIPART_CHUNK_SIZE_IN_MB * 1024 *1024)
		if calc_md5_hash != md5_hash:
			print("md5 checksum mismatch for " + filename + ". Expected: " + md5_hash + ", Actual: " + calc_md5_hash)
			os.remove(filename)
			retry_counter += 1
			if retry_counter > 3:
				print("Giving up after " + str(retry_counter) + " retries for file: " + filename + " ! Please check with support team.")
				time.sleep(5)
				os._exit(1)
			print("[Retry: " + str(retry_counter) + "] Downloading again " + filename)
		else:
			break

def CleanupDir(folderName):
	if os.path.exists(folderName):
		shutil.rmtree(folderName)

def CreateAndChangeDir(folderName):
	if not os.path.exists(folderName):
		os.mkdir(folderName)
	os.chdir(folderName)

def CleanupCreateAndChangeDir(folderName):
	CleanupDir(folderName)
	os.mkdir(folderName)
	os.chdir(folderName)

def ExtractAllGzippedObjects():
	files = [f for f in os.listdir('.') if os.path.isfile(f)]
	for f in files:
		if f.endswith('.tar.gz'):
			tf = tarfile.open(f)
			tf.extractall()
			os.remove(f)
		else:
			if os.path.isfile(f):
				os.remove(f)
			else:
				shutil.rmtree(f)


def calculate_multipart_etag(source_path, chunk_size):

	"""
	calculates a multipart upload etag for amazon s3
	Arguments:
	source_path -- The file to calculate the etage for
	chunk_size -- The chunk size to calculate for.
	"""

	md5s = []
	with open(source_path,'rb') as fp:
		first = True
		while True:
			data = fp.read(chunk_size)
			if not data:
				if first:
					md5s.append(hashlib.md5())
				break
			md5s.append(hashlib.md5(data))
			first = False

	if len(md5s) > 1:
		digests = b"".join(m.digest() for m in md5s)
		new_md5 = hashlib.md5(digests)
		new_etag = '"%s-%s"' % (new_md5.hexdigest(),len(md5s))
	elif len(md5s) == 1: # file smaller than chunk size
		new_etag = '"%s"' % md5s[0].hexdigest()
	else: # empty file
		new_etag = '""'

	return new_etag
				
def run():
	while (True):
		try:
			if(UploadLock() == False):
				print("[" + str(datetime.datetime.now()) + "] Started downloading entire persistence")
				GetEntirePersistenceFromS3()
			else:
				continue

			if(UploadLock() == True):
				print("Upload has been triggered. Downloading StateDelta now can cause inconsistent data. will restart again..")
				continue

			print("Started downloading State-Delta")
			GetStateDeltaFromS3()
			break

		except Exception as e:
			print(e)
			print("Error downloading!! Will try again")
			time.sleep(5)
			continue

	print("[" + str(datetime.datetime.now()) + "] Done!")
	exit(0)

if __name__ == "__main__":
	if len(sys.argv) >= 2:
		if os.path.isabs(sys.argv[1]):
			STORAGE_PATH = sys.argv[1]
		else:
			# Get absolute path w.r.t to script 
			STORAGE_PATH = os.path.join(BASE_PATH, sys.argv[1])

		if len(sys.argv) == 3 and sys.argv[2] == "false":
			Exclude_txnBodies = False
			Exclude_microBlocks = False
	run()
