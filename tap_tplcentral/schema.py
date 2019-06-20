import os
import json

SCHEMAS = {}
FIELD_METADATA = {}

PKS = {
    'customers': ['customer_id'],
    'sku_items': ['item_id'],
    'stock_details': ['receive_item_id'],
    'orders': ['order_id'],
    'order_items': ['order_item_id'],
    'order_packages': ['package_id'],
    'inventory': ['receive_item_id'],
    'stock_summaries': ['receive_item_id']
}


def get_abs_path(path):
    return os.path.join(os.path.dirname(os.path.realpath(__file__)), path)

def get_schemas():
    global SCHEMAS, FIELD_METADATA

    if SCHEMAS:
        return SCHEMAS, FIELD_METADATA

    schemas_path = get_abs_path('schemas')

    file_names = [f for f in os.listdir(schemas_path)
                  if os.path.isfile(os.path.join(schemas_path, f))]

    for file_name in file_names:
        stream_name = file_name[:-5]
        with open(os.path.join(schemas_path, file_name)) as data_file:
            schema = json.load(data_file)
            
        SCHEMAS[stream_name] = schema
        pk = PKS[stream_name]

        metadata = []
        for prop, json_schema in schema['properties'].items():
            if prop in pk:
                inclusion = 'automatic'
            else:
                inclusion = 'available'
            metadata.append({
                'metadata': {
                    'inclusion': inclusion
                },
                'breadcrumb': ['properties', prop]
            })
        FIELD_METADATA[stream_name] = metadata

    return SCHEMAS, FIELD_METADATA
