# AUTOGENERATED! DO NOT EDIT! File to edit: nbs/01_eumetsat.ipynb (unless otherwise specified).

__all__ = ['request_access_token', 'query_data_products', 'format_dt_str', 'identify_available_datasets',
           'dataset_id_to_link', 'json_extract', 'extract_metadata', 'metadata_maps', 'check_valid_request',
           'DownloadManager', 'get_dir_size', 'get_filesize_megabytes', 'eumetsat_filename_to_datetime',
           'compress_downloaded_files', 'upload_compressed_files']

# Cell
import numpy as np
import pandas as pd
import dataset

import FEAutils as hlp
from typing import Union, List
import xmltodict
import dotenv
import datetime
import zipfile
import copy
import os
from io import BytesIO
import re
import glob
import logging
import math
import shutil
import subprocess
from pathlib import Path
import urllib

from requests.auth import HTTPBasicAuth
import requests

from nbdev.showdoc import *
from fastcore.test import test

from satip import utils
from .gcp_helpers import get_eumetsat_filenames, upload_blob

from ipypb import track
from IPython.display import JSON

# Cell
def request_access_token(user_key, user_secret):
    """
    Requests an access token from the EUMETSAT data API

    Parameters:
        user_key: EUMETSAT API key
        user_secret: EUMETSAT API secret

    Returns:
        access_token: API access token

    """

    token_url = 'https://api.eumetsat.int/token'

    r = requests.post(
        token_url,
        auth=requests.auth.HTTPBasicAuth(user_key, user_secret),
        data = {'grant_type': 'client_credentials'},
        headers = {"Content-Type" : "application/x-www-form-urlencoded"}
    )
    access_token = r.json()['access_token']

    return access_token

# Cell
format_dt_str = lambda dt: pd.to_datetime(dt).strftime('%Y-%m-%dT%H:%M:%SZ')

def query_data_products(
    start_date:str='2020-01-01',
    end_date:str='2020-01-02',
    start_index:int=0,
    num_features:int=10_000,
    product_id:str='EO:EUM:DAT:MSG:MSG15-RSS',
) -> requests.models.Response:
    """
    Queries the EUMETSAT data API for the specified data
    product and date-range. The dates will accept any
    format that can be interpreted by `pd.to_datetime`.
    A maximum of 10,000 entries are returned by the API
    so the indexes of the returned entries can be specified.

    Parameters:
        start_date: Start of the query period
        end_date: End of the query period
        start_index: Starting index of returned entries
        num_features: Number of returned entries
        product_id: ID of the EUMETSAT product requested

    Returns:
        r: Response from the request

    """

    search_url = 'https://api.eumetsat.int/data/search-products/os'

    params = {
        'format': 'json',
        'pi': product_id,
        'si': start_index,
        'c': num_features,
        'sort': 'start,time,0',
        'dtstart': format_dt_str(start_date),
        'dtend': format_dt_str(end_date),
    }

    r = requests.get(search_url, params=params)

    assert r.ok, f'Request was unsuccesful: {r.status_code} - {r.text}'

    return r

# Cell
def identify_available_datasets(start_date: str, end_date: str,
                                product_id='EO:EUM:DAT:MSG:MSG15-RSS', log=None):
    """
    Identifies available datasets from the EUMETSAT data
    API for the specified data product and date-range.
    The dates will accept any format that can be
    interpreted by `pd.to_datetime`.

    Parameters:
        start_date: Start of the query period
        end_date: End of the query period
        product_id: ID of the EUMETSAT product requested

    Returns:
        r: Response from the request

    """
    r_json = query_data_products(start_date, end_date,
                                 product_id=product_id).json()

    num_total_results = r_json['properties']['totalResults']
    print(f'identify_available_datasets: found {num_total_results} results from API')
    if log:
        log.info(f'Found {len(num_total_results)} EUMETSAT dataset files')

    if num_total_results < 10_000:
        return r_json['features']

    datasets = r_json['features']

    # need to loop in batches of 10_000 until all results are found
    extra_loops_needed = num_total_results // 10_000

    new_end_date = datasets[-1]['properties']['date'].split('/')[1]
    batch_r_json = []
    num_features = 10_000

    for i in range(extra_loops_needed):

        # ensure the last loop we only get the remaining assets
        if i + 1 < extra_loops_needed:
            num_features = 10_000
        else:
            num_features = num_total_results - len(datasets)

        batch_r_json = query_data_products(start_date, new_end_date,
                                 num_features=num_features,
                                 product_id=product_id).json()
        new_end_date = batch_r_json['features'][-1]['properties']['date'].split('/')[1]
        datasets = datasets + batch_r_json['features']

    assert num_total_results == len(datasets), f'Some features have not been appended - {len(datasets)} / {num_total_results}'

    return datasets

# Cell
def dataset_id_to_link(collection_id, data_id, access_token):
    return f'https://api.eumetsat.int/data/download/collections/{urllib.parse.quote(collection_id)}/products/{urllib.parse.quote(data_id)}' + '?access_token=' + access_token

# Cell
def json_extract(json_obj:Union[dict, list], locators:list):
    extracted_obj = copy.deepcopy(json_obj)

    for locator in locators:
        extracted_obj = extracted_obj[locator]

    return extracted_obj

def extract_metadata(data_dir: str, product_id='EO:EUM:DAT:MSG:MSG15-RSS'):
    with open(f'{data_dir}/EOPMetadata.xml', 'r') as f:
        xml_str = f.read()

    raw_metadata = xmltodict.parse(xml_str)
    metadata_map = metadata_maps[product_id]

    datatypes_to_transform_func = {
        'datetime': pd.to_datetime,
        'str': str,
        'int': int,
        'float': float
    }

    cleaned_metadata = dict()

    for feature, attrs in metadata_map.items():
        location = attrs['location']
        datatype = attrs['datatype']

        value = json_extract(raw_metadata, location)
        formatted_value = datatypes_to_transform_func[datatype](value)

        cleaned_metadata[feature] = formatted_value

    return cleaned_metadata

metadata_maps = {
    'EO:EUM:DAT:MSG:MSG15-RSS': {
        'start_date': {
            'datatype': 'datetime',
            'location': ['eum:EarthObservation', 'om:phenomenonTime', 'gml:TimePeriod', 'gml:beginPosition']
        },
        'end_date': {
            'datatype': 'datetime',
            'location': ['eum:EarthObservation', 'om:phenomenonTime', 'gml:TimePeriod', 'gml:endPosition']
        },
        'result_time': {
            'datatype': 'datetime',
            'location': ['eum:EarthObservation', 'om:resultTime', 'gml:TimeInstant', 'gml:timePosition']
        },
        'platform_short_name': {
            'datatype': 'str',
            'location': ['eum:EarthObservation', 'om:procedure', 'eop:EarthObservationEquipment', 'eop:platform', 'eop:Platform', 'eop:shortName']
        },
        'platform_orbit_type': {
            'datatype': 'str',
            'location': ['eum:EarthObservation', 'om:procedure', 'eop:EarthObservationEquipment', 'eop:platform', 'eop:Platform', 'eop:orbitType']
        },
        'instrument_name': {
            'datatype': 'str',
            'location': ['eum:EarthObservation', 'om:procedure', 'eop:EarthObservationEquipment', 'eop:instrument', 'eop:Instrument', 'eop:shortName']
        },
        'sensor_op_mode': {
            'datatype': 'str',
            'location': ['eum:EarthObservation', 'om:procedure', 'eop:EarthObservationEquipment', 'eop:sensor', 'eop:Sensor', 'eop:operationalMode']
        },
        'center_srs_name': {
            'datatype': 'str',
            'location': ['eum:EarthObservation', 'om:featureOfInterest', 'eop:Footprint', 'eop:centerOf', 'gml:Point', '@srsName']
        },
        'center_position': {
            'datatype': 'str',
            'location': ['eum:EarthObservation', 'om:featureOfInterest', 'eop:Footprint', 'eop:centerOf', 'gml:Point', 'gml:pos']
        },
        'file_name': {
            'datatype': 'str',
            'location': ['eum:EarthObservation', 'om:result', 'eop:EarthObservationResult', 'eop:product', 'eop:ProductInformation', 'eop:fileName', 'ows:ServiceReference', '@xlink:href']
        },
        'file_size': {
            'datatype': 'int',
            'location': ['eum:EarthObservation', 'om:result', 'eop:EarthObservationResult', 'eop:product', 'eop:ProductInformation', 'eop:size', '#text']
        },
        'missing_pct': {
            'datatype': 'float',
            'location': ['eum:EarthObservation', 'eop:metaDataProperty', 'eum:EarthObservationMetaData', 'eum:missingData', '#text']
        },
    }
}

# Cell
def check_valid_request(r: requests.models.Response):
    """
    Checks that the response from the request is valid

    Parameters:
        r: Response object from the request

    """

    class InvalidCredentials(Exception):
        pass

    if r.ok == False:
        if 'Invalid Credentials' in r.text:
            raise InvalidCredentials('The access token passed in the API request is invalid')
        else:
            raise Exception(f'The API request was unsuccesful {r.text} {r.status_code}')

    return

class DownloadManager:
    """
    The DownloadManager class provides a handler for downloading data
    from the EUMETSAT API, managing: retrieval, logging and metadata

    """

    def __init__(self, user_key: str, user_secret: str,
                 data_dir: str, metadata_db_fp: str, log_fp: str,
                 main_logging_level: str='DEBUG', slack_logging_level: str='CRITICAL',
                 slack_webhook_url: str=None, slack_id: str=None,
                 bucket_name=None, bucket_prefix=None, logger_name='EUMETSAT Download'):
        """
        Initialises the download manager by:
        * Setting up the logger
        * Requesting an API access token
        * Configuring the download directory
        * Connecting to the metadata database
        * Adding satip helper functions

        Parameters:
            user_key: EUMETSAT API key
            user_secret: EUMETSAT API secret
            data_dir: Path to the directory where the satellite data will be saved
            metadata_db_fp: Path to where the metadata database is stored/will be saved
            log_fp: Filepath where the logs will be stored
            main_logging_level: Logging level for file and Jupyter
            slack_logging_level: Logging level for Slack
            slack_webhook_url: Webhook for the log Slack channel
            slack_id: Option user-id to mention in Slack
            bucket_name: (Optional) Google Cloud Storage bucket name to check for existing files
            bucket_prefix: (Optional) Prefix for cloud bucket files

        Returns:
            download_manager: Instance of the DownloadManager class

        """

        # Configuring the logger
        self.logger = utils.set_up_logging(logger_name, log_fp,
                                           main_logging_level, slack_logging_level,
                                           slack_webhook_url, slack_id)

        self.logger.info(f'********** Download Manager Initialised **************')

        # Requesting the API access token
        self.user_key = user_key
        self.user_secret = user_secret

        self.request_access_token()

        # Configuring the data directory
        self.data_dir = data_dir

        if not os.path.exists(self.data_dir):
            os.makedirs(self.data_dir)

        # Initialising the metadata database
        self.metadata_db = dataset.connect(f'sqlite:///{metadata_db_fp}')
        self.metadata_table = self.metadata_db['metadata']

        # Adding satip helper functions
        self.identify_available_datasets = identify_available_datasets
        self.query_data_products = query_data_products

        # Google Cloud integration
        self.bucket_name = bucket_name
        self.bucket_prefix = bucket_prefix
        self.bucket_filenames = None

        if bucket_name:
            print(f'Checking files in GCP bucket {bucket_name}, this will take a few seconds')
            filenames = get_eumetsat_filenames(bucket_name, prefix=bucket_prefix)
            self.bucket_filenames = [re.match("([A-Z\d.]+-){6}", filename)[0][:-1] for filename in filenames]

        return


    def request_access_token(self, user_key=None, user_secret=None):
        """
        Requests an access token from the EUMETSAT data API.
        If no key or secret are provided then they will default
        to the values provided in the download manager initialisation

        Parameters:
            user_key: EUMETSAT API key
            user_secret: EUMETSAT API secret

        Returns:
            access_token: API access token

        """

        if user_key is None:
            user_key = self.user_key
        if user_secret is None:
            user_secret = self.user_secret

        self.access_token = request_access_token(user_key, user_secret)

        return

    def download_single_dataset(self, data_link:str):
        """
        Downloads a single dataset from the EUMETSAT API

        Parameters:
            data_link: Url link for the relevant dataset

        """

        params = {
            'access_token': self.access_token
        }

        r = requests.get(data_link, params=params)
        check_valid_request(r)

        zipped_files = zipfile.ZipFile(BytesIO(r.content))
        zipped_files.extractall(f'{self.data_dir}')

        return


    def check_if_downloaded(self, filenames: List[str]):
        """Checks which files should be downloaded based on
        local file contents and a cloud storage bucket, if specified.

        Parameters:
            filenames: List of filename strings

        Returns:
            List of filenames to download
        """
        in_bucket = []
        local = []
        download = []

        for filename in filenames:
            # get first part of filename for matching
            match = re.match("([A-Z\d.]+-){6}", filename)[0][:-1]

            if self.bucket_name:
                if match in self.bucket_filenames:
                    in_bucket.append(filename)
                    if f'{filename}.nat' in os.listdir(self.data_dir):
                        local.append(filename)
                    continue

            if f'{filename}.nat' in os.listdir(self.data_dir):
                local.append(filename)
                continue

            download.append(filename)

        if self.bucket_name:
            self.logger.info(f'{len(filenames)} files queried, {len(in_bucket)} found in bucket, {len(local)} found in {self.data_dir}, {len(download)} to download.')
        else:
            self.logger.info(f'{len(filenames)} files queried, {len(local)} found in {self.data_dir}, {len(download)} to download.')

        return download, local


    def download_date_range(self, start_date:str, end_date:str, product_id='EO:EUM:DAT:MSG:MSG15-RSS'):
        """
        Downloads a set of dataset from the EUMETSAT API
        in the defined date range and specified product

        Parameters:
            start_date: Start of the requested data period
            end_date: End of the requested data period
            product_id: ID of the EUMETSAT product requested

        """

        datasets = identify_available_datasets(start_date, end_date, product_id=product_id)
        df_new_metadata = self.download_datasets(datasets, product_id=product_id)

        return df_new_metadata


    def download_datasets(self, datasets, product_id='EO:EUM:DAT:MSG:MSG15-RSS', download_all=True):
        """
        Downloads a set of dataset from the EUMETSAT API
        in the defined date range and specified product

        Parameters:
            datasets: list of datasets returned by `identify_available_datasets`

        """

        # Identifying dataset ids to download
        dataset_ids = sorted([dataset['id'] for dataset in datasets])

        # Check which datasets to download
        download_ids, local_ids = self.check_if_downloaded(dataset_ids)
        # Downloading specified datasets
        if not dataset_ids:
            self.logger.info('No files will be downloaded. Set DownloadManager bucket_name argument for local download')
            return

        all_metadata = []

        for dataset_id in track(dataset_ids):
            dataset_link = dataset_id_to_link(product_id, dataset_id, access_token=self.access_token)
            # Download the raw data
            if (dataset_id in download_ids) or (download_all == True):
                try:
                    self.download_single_dataset(dataset_link)
                except:
                    self.logger.info('The EUMETSAT access token has been refreshed')
                    self.request_access_token()
                    dataset_link = dataset_id_to_link(product_id, dataset_id, access_token=self.access_token)
                    self.download_single_dataset(dataset_link)

            # Extract and save metadata
            #dataset_metadata = extract_metadata(self.data_dir, product_id=product_id)
            #dataset_metadata.update({'downloaded': pd.Timestamp.now()})
            #all_metadata += [dataset_metadata]

            # Delete old metadata files
            for xml_file in ['EOPMetadata.xml', 'manifest.xml']:
                xml_filepath = f'{self.data_dir}/{xml_file}'

                if os.path.isfile(xml_filepath):
                    os.remove(xml_filepath)

        df_new_metadata = pd.DataFrame(all_metadata)

        return df_new_metadata

    # First run, we have no data
    try:
        get_df_metadata = lambda self: pd.DataFrame(self.metadata_table.all()).set_index('id')
    except:
        get_df_metadata = None

# Cell
def get_dir_size(directory='.'):
    total_size = 0

    for dirpath, dirnames, filenames in os.walk(directory):
        for f in filenames:
            fp = os.path.join(dirpath, f)

            if not os.path.islink(fp):
                total_size += os.path.getsize(fp)

    return total_size

# Cell
def get_filesize_megabytes(filename):
    """Returns filesize in megabytes"""
    filesize_bytes = os.path.getsize(filename)
    return filesize_bytes / 1E6


def eumetsat_filename_to_datetime(inner_tar_name):
    """Takes a file from the EUMETSAT API and returns
    the date and time part of the filename"""

    p = re.compile('^MSG[23]-SEVI-MSG15-0100-NA-(\d*)\.')
    title_match = p.match(inner_tar_name)
    date_str = title_match.group(1)
    return datetime.datetime.strptime(date_str, "%Y%m%d%H%M%S")

# Cell
def compress_downloaded_files(data_dir, compressed_dir, log=None):
    """
    Compresses downloaded files, stores them locally,
    and ensures they are approximately the correct filesize.
    Uses pbzip2 for compression.

        Parameters:
            data_dir: (string), directory path containing raw downloaded files from EUMETSAT API
            compressed_dir: (string), directory path for compressed .nat files
            log: (bool), flag to enable logging

        Returns:
            -
    """
    NATIVE_FILESIZE_MB = 102.210123
    EXTENSION = '.bz2'

    full_native_filenames = glob.glob(os.path.join(data_dir, '*.nat'))
    print(f'Found {len(full_native_filenames)} native files.')
    if log:
        log.info(f'Found {len(full_native_filenames)} native files.')

    for full_native_filename in full_native_filenames:
        # Check filesize is correct
        native_filesize_mb = get_filesize_megabytes(full_native_filename)

        if not math.isclose(native_filesize_mb, NATIVE_FILESIZE_MB, abs_tol=1):
            msg = f'Filesize incorrect for {full_native_filename}!  Expected {NATIVE_FILESIZE_MB} MB.  Actual = {native_filesize_mb} MB.'
            if log:
                log.error(msg)

        if log:
            log.debug(f'Compressing {full_native_filename}')

        completed_process = subprocess.run(['pbzip2', '-5', full_native_filename])
        try:
            completed_process.check_returncode()
        except:
            if log:
                log.exception('Compression failed!')
            print('Compression failed!')
            raise

        full_compressed_filename = full_native_filename + EXTENSION
        compressed_filesize_mb = get_filesize_megabytes(full_compressed_filename)
        if log:
            log.debug(f'Filesizes: Before compression = {native_filesize_mb} MB. After compression = {compressed_filesize_mb} MB.  Compressed file is {compressed_filesize_mb/native_filesize_mb} x the size of the uncompressed file.')

        base_native_filename = os.path.basename(full_native_filename)
        dt = eumetsat_filename_to_datetime(base_native_filename)

        # Creating compressed_dir if not already made
        if not os.path.exists(compressed_dir):
            os.makedirs(compressed_dir)

        new_dst_path = os.path.join(compressed_dir, dt.strftime("%Y/%m/%d/%H/%M"))
        if not os.path.exists(new_dst_path):
            os.makedirs(new_dst_path)

        new_dst_full_filename = os.path.join(new_dst_path, base_native_filename + EXTENSION)
        if log:
            log.debug(f'Moving {full_compressed_filename} to {new_dst_full_filename}')

        if os.path.exists(new_dst_full_filename):
            if log:
                log.debug(f'{new_dst_full_filename} already exists.  Deleting old file')
            os.remove(new_dst_full_filename)
        shutil.move(src=full_compressed_filename, dst=new_dst_path)
    print(f'Moved and compressed {len(full_native_filenames)} files to {compressed_dir}')

# Cell
def upload_compressed_files(compressed_dir, BUCKET_NAME, PREFIX, log=None):
    """Uploads compressed native files to a Google Cloud storage bucket

    For example,
    compressed_dir:  /home/srv/data/intermediate/
    bucket name: solar-pv-nowcasting-data
    prefix:      satellite/EUMETSAT/SEVIRI_RSS/native/

    With some files like:
    /home/srv/data/intermediate/2018/01/01/01/23/04/MSG3-SEVI-MSG15-0100-NA-20191001120415.883000000Z-NA.nat.bz2

    Would upload the files to:
    gs://solar-pv-nowcasting-data/satellite/EUMETSAT/SEVIRI_RSS/native/2018/01/01/01/23/04/MSG3-SEVI-MSG15-0100-NA-20191001120415.883000000Z-NA.nat.bz2
    etc

        Parameters:
            compressed_dir: (str), directory where compressed files are stored locally
            BUCKET_NAME: (str), name of Google Cloud storage bucket
            PREFIX: (str), string prefix to use as part of the bucket storage path

        Returns:
            -
    """

    paths = Path(compressed_dir).rglob("*.nat.bz2")
    full_compressed_files = [x for x in paths if x.is_file()]
    if log:
        log.info(f"Found {len(full_compressed_files)} compressed files.")
        log.info(f"Checking cloud storage bucket")

    filenames = get_eumetsat_filenames(BUCKET_NAME, prefix=PREFIX)
    bucket_filenames = [re.match("([A-Z\d.]+-){6}", filename)[0][:-1] + '.nat.bz2' for filename in filenames]

    for file in full_compressed_files:
        if file in bucket_filenames:
            print(f'{file} in cloud bucket, skipping upload')
            log.info(f'{file} in cloud bucket, skipping upload')
        else:
            rel_path = os.path.relpath(file.absolute(), compressed_dir)
            upload_blob(
                bucket_name=BUCKET_NAME,
                source_file_name=file.absolute(),
                destination_blob_name=rel_path,
                prefix=PREFIX,
            )