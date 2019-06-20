from datetime import datetime
import math
import singer
from singer import metrics, metadata, Transformer, utils
from tap_tplcentral.transform import transform_json, convert

LOGGER = singer.get_logger()


def write_schema(catalog, stream_name):
    stream = catalog.get_stream(stream_name)
    schema = stream.schema.to_dict()
    singer.write_schema(stream_name, schema, stream.key_properties)


def process_records(catalog,
                    stream_name,
                    records,
                    time_extracted,
                    bookmark_field=None,
                    bookmark_type=None,
                    max_bookmark_value=None,
                    last_datetime=None,
                    last_integer=None,
                    parent=None,
                    parent_id=None):
    stream = catalog.get_stream(stream_name)
    schema = stream.schema.to_dict()
    stream_metadata = metadata.to_map(stream.metadata)

    with metrics.record_counter(stream_name) as counter:
        for record in records:
            # If child object, add parent_id to record
            if parent_id and parent:
                record[parent + '_id'] = parent_id

            # Reset max_bookmark_value to new value if higher
            if bookmark_field and (bookmark_field in record):
                if (max_bookmark_value is None) or \
                    (record[bookmark_field] > max_bookmark_value):
                    max_bookmark_value = record[bookmark_field]

            # Transform record for Singer.io
            with Transformer() as transformer:
                record = transformer.transform(record,
                                                schema,
                                                stream_metadata)

            if bookmark_field and (bookmark_field in record):
                if bookmark_type == 'integer':
                    # Keep only records whose bookmark is after the last_integer
                    if record[bookmark_field] >= last_integer:
                        singer.write_record(stream_name, record, time_extracted=time_extracted)
                        counter.increment()
                elif bookmark_type == 'datetime':
                    # Keep only records whose bookmark is after the last_datetime
                    if record[bookmark_field] >= last_datetime:
                        singer.write_record(stream_name, record, time_extracted=time_extracted)
                        counter.increment()
            else:
                singer.write_record(stream_name, record, time_extracted=time_extracted)
                counter.increment()
        return max_bookmark_value


def get_bookmark(state, stream, default):
    if (state is None) or ('bookmarks' not in state):
        return default
    return (
        state
        .get('bookmarks', {})
        .get(stream, default)
    )

def write_bookmark(state, stream, value):
    if 'bookmarks' not in state:
        state['bookmarks'] = {}
    state['bookmarks'][stream] = value
    singer.write_state(state)

def sync_endpoint(client,
                  catalog,
                  state,
                  start_date,
                  stream_name,
                  path,
                  data_key,
                  static_params,
                  bookmark_path,
                  bookmark_query_field,
                  bookmark_field,
                  bookmark_type=None,
                  id_field=None,
                  parent=None,
                  parent_id=None):
    bookmark_path = bookmark_path + [bookmark_field]

    last_datetime = None
    last_integer = None
    if bookmark_type == 'integer':
        last_integer = get_bookmark(state, stream_name, 0)
        max_bookmark_value = last_integer
    else:
        last_datetime = get_bookmark(state, stream_name, start_date)
        max_bookmark_value = last_datetime

    ids = []

    # Stores parent object ids in id_bag for children
    def transform(record, id_field='id'):
        _id = record.get(id_field)
        if _id:
            ids.append(_id)
        return record

    write_schema(catalog, stream_name)

    # pagination: loop thru all pages of data
    page = 1
    total_pages = 1  # initial value, set with first API call
    while page <= total_pages:
        params = {
            'pgnum': page,
            **static_params # adds in endpoint specific, sort, filter params
        }

        if 'pgsiz' in params:
            page_size = params['pgsiz']
        else:
            page_size = 100

        # Resource Query Language (RQL) is used to filter data. Reference: http://api.3plcentral.com/rels/rql
        if bookmark_query_field:
            if 'rql' in params:
                if bookmark_type == 'datetime':
                    params['rql'] = '{};{}=ge={}'.format(params['rql'], bookmark_query_field, last_datetime)
                elif bookmark_type == 'integer':
                    params['rql'] = '{};{}=ge={}'.format(params['rql'], bookmark_query_field, last_integer)
            else:
                if bookmark_type == 'datetime':
                    params['rql'] = '{}=ge={}'.format(bookmark_query_field, last_datetime)
                elif bookmark_type == 'integer':
                    params['rql'] = '{}=ge={}'.format(bookmark_query_field, last_integer)

        LOGGER.info('{} - Sync start'.format(
            stream_name,
            'since: {}, '.format(last_datetime) if bookmark_query_field else ''))

        # Squash params to query-string params
        querystring = '&'.join(['%s=%s' % (key, value) for (key, value) in params.items()])

        # For testing/debugging:
        # LOGGER.info('stream_name = {}'.format(stream_name))
        # LOGGER.info('path = {}'.format(path))
        # LOGGER.info('querystring = {}'.format(querystring))

        # Get data, API request
        data = client.get(
            path,
            querystring=querystring,
            endpoint=stream_name)

        # Transform raw data with transform_json from transform.py
        transformed_data = transform_json(data, data_key)[convert(data_key)]

        time_extracted = utils.now()

        max_bookmark_value = process_records(
            catalog=catalog,
            stream_name=stream_name,
            records=[transform(rec, id_field) for rec in transformed_data],
            time_extracted=time_extracted,
            bookmark_field=bookmark_field,
            bookmark_type=bookmark_type,
            max_bookmark_value=max_bookmark_value,
            last_datetime=last_datetime,
            last_integer=last_integer,
            parent=parent,
            parent_id=parent_id)

        if bookmark_field:
            write_bookmark(state,
                           stream_name,
                           max_bookmark_value)

        # set page and total_pages for pagination
        if 'TotalResults' in data:
            if data['TotalResults'] < page_size:
                total_pages = 1
            else:
                total_results = data['TotalResults']
                total_pages = math.ceil(total_results / page_size)
        else:
            total_pages = 1
        LOGGER.info('{} - Synced - page: {}, total pages: {}'.format(
            stream_name,
            page,
            total_pages))
        page = page + 1

    return ids


def sync_stream(client,
                catalog,
                state,
                start_date,
                id_bag,
                stream_name,
                endpoint_config,
                bookmark_path=None,
                id_path=None,
                parent_id=None):
    if not bookmark_path:
        bookmark_path = [stream_name]
    if not id_path:
        id_path = []

    path = endpoint_config.get('path').format(*id_path)
    stream_ids = sync_endpoint(
        client=client,
        catalog=catalog,
        state=state,
        start_date=start_date,
        stream_name=stream_name,
        path=path,
        data_key=endpoint_config.get('data_path', stream_name),
        static_params=endpoint_config.get('params', {}),
        bookmark_path=bookmark_path,
        bookmark_query_field=endpoint_config.get('bookmark_query_field'),
        bookmark_field=endpoint_config.get('bookmark_field'),
        bookmark_type=endpoint_config.get('bookmark_type'),
        id_field=endpoint_config.get('id_field'),
        parent=endpoint_config.get('parent'),
        parent_id=parent_id)

    if endpoint_config.get('store_ids'):
        id_bag[stream_name] = stream_ids
    
    children = endpoint_config.get('children')
    if children:
        for child_stream_name, child_endpoint_config in children.items():
            for _id in stream_ids:
                sync_stream(
                    client=client,
                    catalog=catalog,
                    state=state,
                    start_date=start_date,
                    id_bag=id_bag,
                    stream_name=child_stream_name,
                    endpoint_config=child_endpoint_config,
                    bookmark_path=bookmark_path + [_id, child_stream_name],
                    id_path=id_path + [_id],
                    parent_id=_id)


# Review catalog and make a list of selected streams
def get_selected_streams(catalog):
    selected_streams = set()
    for stream in catalog.streams:
        mdata = metadata.to_map(stream.metadata)
        root_metadata = mdata.get(())
        if root_metadata and root_metadata.get('selected') is True:
            selected_streams.add(stream.tap_stream_id)
    return list(selected_streams)


# Review last_stream (last currently syncing stream), if any,
#  and continue where it left off in the selected streams.
# Or begin from the beginning, if no last_stream, and sync
#  all selected steams.
# Returns should_sync_stream (true/false) and last_stream.
def should_sync_stream(selected_streams, last_stream, stream_name):
    if last_stream == stream_name or last_stream is None:
        if last_stream is not None:
            last_stream = None
        if stream_name in selected_streams:
            return True, last_stream
    return False, last_stream


def sync(client, config, catalog, state):
    if 'start_date' in config:
        start_date = config['start_date']
    if 'customer_id' in config:
        customer_id = config['customer_id']
    if 'facility_id' in config:
        facility_id = config['facility_id']
    
    selected_streams = get_selected_streams(catalog)
    if not selected_streams:
        return
    
    last_stream = singer.get_currently_syncing(state)
    id_bag = {}

    endpoints = {
        'customers': {
            'path': 'customers',
            'params': {
                'pgsiz': 100,
                'sort': 'ReadOnly.CreationDate'
            },
            'data_path': 'ResourceList',
            'bookmark_field': 'creation_date',
            'bookmark_type': 'datetime',
            'id_field': 'customer_id',
            'store_ids': True,
            'children': {
               'customer_items': {
                    'path': 'customers/{}/items',
                    'params': {
                        'pgsiz': 100,
                        'sort': 'ReadOnly.lastModifiedDate'
                    },
                    'data_path': 'ResourceList',
                    'bookmark_field': 'last_modified_date',
                    'bookmark_type': 'datetime',
                    'bookmark_query_field': 'ReadOnly.lastModifiedDate',
                    'id_field': 'item_id',
                    'parent': 'customer'
                },
                'customer_stock_details': {
                    'path': 'inventory/stockdetails',
                    'params': {
                        'pgsiz': 100,
                        'customerid': customer_id,
                        'facilityid': facility_id,
                        'sort': 'receivedDate'
                    },
                    'data_path': 'ResourceList',
                    'bookmark_field': 'received_date',
                    'bookmark_type': 'datetime',
                    'bookmark_query_field': 'receivedDate',
                    'id_field': 'receive_item_id',
                    'parent': 'customer'
                }
            }
        },
        
        'inventory': {
            'path': 'inventory',
            'params': {
                'pgsiz': 500,
                'sort': 'receivedDate'
            },
            'data_path': 'ResourceList',
            'bookmark_field': 'received_date',
            'bookmark_type': 'datetime',
            'bookmark_query_field': 'receivedDate',
            'id_field': 'receive_item_id'
        },

        'orders': {
            'path': 'orders',
            'params': {
                'pgsiz': 500,
                'detail': 'BillingDetails,SavedElements,Contacts,ProposedBilling,OutboundSerialNumbers',
                'sort': 'ReadOnly.lastModifiedDate'
            },
            'data_path': 'ResourceList',
            'bookmark_field': 'last_modified_date',
            'bookmark_type': 'datetime',
            'bookmark_query_field': 'ReadOnly.lastModifiedDate',
            'id_field': 'order_id',
            'store_ids': True,
            'children': {
               'order_items': {
                    'path': 'orders/{}/items',
                    'params': {
                        'detail': 'All'
                    },
                    'data_path': 'ResourceList',
                    'bookmark_field': 'order_item_id',
                    'bookmark_type': 'integer',
                    'id_field': 'order_item_id',
                    'parent': 'order'
                },
                'order_packages': {
                    'path': 'orders/{}/packages',
                    'params': {},
                    'data_path': 'ResourceList',
                    'bookmark_field': 'create_date',
                    'bookmark_type': 'datetime',
                    'id_field': 'package_id',
                    'parent': 'order'
                },
            }
        },
        
        'stock_summaries': {
            'path': 'inventory/stocksummaries',
            'params': {
                'pgsiz': 500
            },
            'data_path': 'Summaries'
        }
    }

    for stream_name, endpoint_config in endpoints.items():
        should_stream, last_stream = should_sync_stream(selected_streams,
                                                        last_stream,
                                                        stream_name)
        if should_stream:
            singer.set_currently_syncing(state, stream_name)
            sync_stream(
                client=client,
                catalog=catalog,
                state=state,
                start_date=start_date,
                id_bag=id_bag,
                stream_name=stream_name,
                endpoint_config=endpoint_config)
