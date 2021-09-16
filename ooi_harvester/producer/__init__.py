import os
import json
import datetime
from typing import List
import textwrap

import fsspec
from loguru import logger
import pandas as pd
import dask.dataframe as dd
from siphon.catalog import TDSCatalog

from ..config import METADATA_BUCKET, HARVEST_CACHE_BUCKET
from ..utils.conn import request_data, check_zarr, send_request
from ..utils.parser import (
    estimate_size_and_time,
    get_storage_options,
    parse_uframe_response,
)
from ..utils.compute import map_concurrency
from ooi_harvester.metadata.fetcher import fetch_instrument_streams_list
from ooi_harvester.metadata.utils import get_catalog_meta
from ooi_harvester.utils.conn import get_toc
from ooi_harvester.metadata import get_ooi_streams_and_parameters
from ooi_harvester.producer.models import StreamHarvest
from ooi_harvester.utils.parser import filter_and_parse_datasets


def fetch_streams_list(stream_harvest: StreamHarvest) -> list:
    instruments = get_toc()['instruments']
    filtered_instruments = [
        i
        for i in instruments
        if i['reference_designator'] == stream_harvest.instrument
    ]
    streams_df, _ = get_ooi_streams_and_parameters(filtered_instruments)
    # Only get science stream
    streams_json = streams_df[
        streams_df.stream_type.str.match('Science')
    ].to_json(orient='records')
    streams_list = json.loads(streams_json)
    return streams_list


def request_axiom_catalog(stream_dct):
    axiom_ooi_catalog = TDSCatalog(
        'http://thredds.dataexplorer.oceanobservatories.org/thredds/catalog/ooigoldcopy/public/catalog.xml'
    )
    stream_name = stream_dct['table_name']
    ref = axiom_ooi_catalog.catalog_refs[stream_name]
    catalog_dict = get_catalog_meta((stream_name, ref))
    return catalog_dict


def create_catalog_request(
    stream_dct,
    start_dt=None,
    end_dt=None,
    refresh=False,
    existing_data_path=None,
):
    """Creates a catalog request to the gold copy"""
    # TODO: Allow for filtering down datasets
    beginTime = pd.to_datetime(stream_dct['beginTime'])
    endTime = pd.to_datetime(stream_dct['endTime'])

    zarr_exists = False
    if not refresh:
        if existing_data_path is not None:
            zarr_exists, last_time = check_zarr(
                os.path.join(existing_data_path, stream_dct['table_name']),
                storage_options=get_storage_options(existing_data_path),
            )
        else:
            raise ValueError(
                "Please provide existing data path when not refreshing."
            )

    if zarr_exists:
        start_dt = last_time + datetime.timedelta(seconds=1)
        end_dt = datetime.datetime.utcnow()

    if start_dt:
        if not isinstance(start_dt, datetime.datetime):
            raise TypeError(f"{start_dt} is not datetime.")
        beginTime = start_dt
    if end_dt:
        if not isinstance(end_dt, datetime.datetime):
            raise TypeError(f"{end_dt} is not datetime.")
        endTime = end_dt

    catalog_dict = request_axiom_catalog(stream_dct)
    filtered_catalog_dict = filter_and_parse_datasets(catalog_dict)
    download_cat = os.path.join(
        catalog_dict['base_tds_url'],
        'thredds/fileServer/ooigoldcopy/public',
        catalog_dict['stream_name'],
    )
    result_dict = {
        'thredds_catalog': catalog_dict['catalog_url'],
        'download_catalog': download_cat,
        'status_url': os.path.join(download_cat, 'status.txt'),
        'request_dt': catalog_dict['retrieved_dt'],
        'data_size': filtered_catalog_dict['total_data_bytes'],
        'units': {'data_size': 'bytes', 'request_dt': 'UTC'},
    }
    return {
        "stream_name": catalog_dict["stream_name"],
        "catalog_url": catalog_dict["catalog_url"],
        "base_tds_url": catalog_dict["base_tds_url"],
        "async_url": download_cat,
        "result": result_dict,
        "stream": stream_dct,
        "zarr_exists": zarr_exists,
        "datasets": filtered_catalog_dict['datasets'],
        "provenance": filtered_catalog_dict['provenance'],
        "params": {
            "beginDT": beginTime.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "endDT": endTime.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "include_provenance": True,
        }
    }


def create_request_estimate(
    stream_dct,
    start_dt=None,
    end_dt=None,
    refresh=False,
    existing_data_path=None,
):
    beginTime = pd.to_datetime(stream_dct['beginTime'])
    endTime = pd.to_datetime(stream_dct['endTime'])

    zarr_exists = False
    if not refresh:
        if existing_data_path is not None:
            zarr_exists, last_time = check_zarr(
                os.path.join(existing_data_path, stream_dct['table_name']),
                storage_options=get_storage_options(existing_data_path),
            )
        else:
            raise ValueError(
                "Please provide existing data path when not refreshing."
            )

    if zarr_exists:
        start_dt = last_time + datetime.timedelta(seconds=1)
        end_dt = datetime.datetime.utcnow()

    if start_dt:
        if not isinstance(start_dt, datetime.datetime):
            raise TypeError(f"{start_dt} is not datetime.")
        beginTime = start_dt
    if end_dt:
        if not isinstance(end_dt, datetime.datetime):
            raise TypeError(f"{end_dt} is not datetime.")
        endTime = end_dt

    response, request_dict = request_data(
        stream_dct['platform_code'],
        stream_dct['mooring_code'],
        stream_dct['instrument_code'],
        stream_dct['method'],
        stream_dct['stream'],
        beginTime,
        endTime,
        estimate=True,
    )
    if response:
        table_name = f"{stream_dct['reference_designator']}-{stream_dct['method']}-{stream_dct['stream']}"
        text = textwrap.dedent(
            """\
        *************************************
        {0}
        -------------------------------------
        {1}
        *************************************
        """
        ).format
        if "requestUUID" in response:
            m = estimate_size_and_time(response)
            request_dict["params"].update({"estimate_only": "false"})
            request_dict.update(
                {
                    "stream": stream_dct,
                    "estimated": response,
                    "zarr_exists": zarr_exists,
                }
            )
            logger.debug(text(table_name, m))
        else:
            m = "Skipping... Data not available."
            request_dict.update({"stream": stream_dct, "estimated": response})
            logger.debug(text(table_name, m))

        return request_dict


def _sort_and_filter_estimated_requests(estimated_requests):
    """Internal sorting function"""

    success_requests = sorted(
        filter(
            lambda req: "requestUUID" in req['estimated'], estimated_requests
        ),
        key=lambda i: i['stream']['count'],
    )
    failed_requests = list(
        filter(
            lambda req: "requestUUID" not in req['estimated'],
            estimated_requests,
        )
    )

    return {
        'success_requests': success_requests,
        'failed_requests': failed_requests,
    }


def perform_request(req, refresh=False, goldcopy=False):
    TODAY_DATE = datetime.datetime.utcnow()
    name = req['stream']['table_name']

    refresh_text = 'refresh' if refresh else 'daily'
    datestr = f"{TODAY_DATE:%Y%m}"
    fname = f"{name}__{datestr}__{refresh_text}"
    fs = fsspec.filesystem(
        HARVEST_CACHE_BUCKET.split(":")[0],
        **get_storage_options(HARVEST_CACHE_BUCKET),
    )
    fpath = os.path.join(HARVEST_CACHE_BUCKET, 'ooinet-requests', fname)

    if fs.exists(fpath):
        logger.info(
            f"Already requested {name} on "
            f"{datestr} for {refresh_text} ({fpath})"
        )
        with fs.open(fpath, mode='r') as f:
            response = json.load(f)
    else:
        logger.info(f"Requesting {name}")
        response = dict(
            result=parse_uframe_response(
                send_request(req["url"], params=req["params"])
            ),
            **req,
        )
        with fs.open(fpath, mode='w') as f:
            json.dump(response, f)

    return response


def perform_estimates(instrument_rd, refresh, existing_data_path):
    streams_list = fetch_instrument_streams_list(instrument_rd)
    estimated_requests = map_concurrency(
        create_request_estimate,
        streams_list,
        func_kwargs=dict(
            refresh=refresh, existing_data_path=existing_data_path
        ),
        max_workers=50,
    )
    estimated_dict = _sort_and_filter_estimated_requests(estimated_requests)
    success_requests = estimated_dict['success_requests']
    return success_requests


def fetch_harvest(instrument_rd, refresh, existing_data_path):
    success_requests = perform_estimates(
        instrument_rd, refresh, existing_data_path
    )
    request_responses = []
    if len(success_requests) > 0:
        request_responses = [
            perform_request(req, refresh) for req in success_requests
        ]
    return request_responses
