# -*- coding: utf-8 -*-
#
# Copyright (C) 2023 CERN.
#
# Zenodo-RDM is free software; you can redistribute it and/or modify
# it under the terms of the MIT License; see LICENSE file for more details.
"""OpenAIRE celery tasks."""

import json
from datetime import datetime

from celery import shared_task
from flask import current_app
from invenio_access.permissions import system_identity
from invenio_cache import current_cache
from invenio_rdm_records.proxies import current_rdm_records_service as records_service

from .errors import OpenAIRERequestError
from .serializers import OpenAIREV1Serializer
from .utils import (
    openaire_datasource_id,
    openaire_original_id,
    openaire_request_factory,
    openaire_type,
)


@shared_task(
    ignore_result=True,
    max_retries=6,
    default_retry_delay=4 * 60 * 60,
    rate_limit="100/m",
)
def openaire_direct_index(record_id, retry=True):
    """Send record for direct indexing at OpenAIRE.

    :param record_id: Record Metadata UUID.
    :type record_id: str
    """
    try:
        record = records_service.read(system_identity, record_id)

        # Bail out if not an OpenAIRE record.
        if not (openaire_type(record.data)):
            return

        # Serialize record for OpenAIRE indexing
        serializer = OpenAIREV1Serializer()
        serialized_record = serializer.dump_obj(record.data)

        # Build the request
        openaire_api_url = current_app.config["OPENAIRE_API_URL"]
        url = f"{openaire_api_url}/feedObject"
        request = openaire_request_factory()
        res = request.post(url, data=json.dumps(serialized_record), timeout=10)

        if not res.ok:
            raise OpenAIRERequestError(res.text)

        beta_url = current_app.config.get("OPENAIRE_API_URL_BETA")
        if beta_url:
            beta_endpoint = f"{beta_url}/feedObject"
            res_beta = request.post(
                beta_endpoint, data=json.dumps(serialized_record), timeout=10
            )

            if not res_beta.ok:
                raise OpenAIRERequestError(res_beta.text)
        current_cache.delete(f"openaire_direct_index:{record_id}")
    except Exception as exc:
        current_cache.set(
            f"openaire_direct_index:{record_id}", datetime.now(), timeout=-1
        )
        if retry:
            openaire_direct_index.retry(exc=exc)
        else:
            raise exc


@shared_task(
    ignore_result=True,
    max_retries=6,
    default_retry_delay=4 * 60 * 60,
    rate_limit="100/m",
)
def openaire_delete(record_id=None):
    """Delete record from OpenAIRE index.

    :param record_id: Record Metadata UUID.
    :type record_id: str
    """
    try:
        record = records_service.read(system_identity, record_id)
        resource_type = record.data.get("metadata", {}).get("resource_type")

        if record and not resource_type:
            # record was deleted, find last revision with metadata
            record = next(
                r._record
                for r in record._record.get_records_by_parent(record._record.parent)
                if r.data.get("metadata", {}).get("resource_type")
            )

        if not record:
            raise OpenAIRERequestError("Could not resolve record.")

        original_id = openaire_original_id(record.data)[1]
        datasource_id = openaire_datasource_id(record.data)

        params = {"originalId": original_id, "collectedFromId": datasource_id}
        req = openaire_request_factory()
        res = req.delete(
            current_app.config["OPENAIRE_API_URL"], data=json.dumps(params)
        )

        if not res.ok:
            raise OpenAIRERequestError(res.text)

        if current_app.config["OPENAIRE_API_URL_BETA"]:
            res_beta = req.delete(
                current_app.config["OPENAIRE_API_URL_BETA"], data=json.dumps(params)
            )

            if not res_beta.ok:
                raise OpenAIRERequestError(res.text)

        # Remove from failures cache
        current_cache.delete(f"openaire_direct_index:{record_id}")

    except Exception as exc:
        current_cache.set(
            f"openaire_direct_index:{record_id}", datetime.now(), timeout=-1
        )
        openaire_delete.retry(exc=exc)


@shared_task
def retry_openaire_failures():
    """Retries failed OpenAIRE indexing/deletion operations."""
    cache = current_cache.cache
    failed_records = cache._write_client.keys(
        cache.key_prefix + "openaire_direct_index:*"
    )
    for key in failed_records:
        record_id = key.decode().split("openaire_direct_index:")[1]
        record = records_service.read(system_identity, record_id)
        resource_type = record.data.get("metadata", {}).get("resource_type")
        if resource_type:
            openaire_direct_index.delay(record_id, retry=False)
        else:
            openaire_delete.delay(record_id)
