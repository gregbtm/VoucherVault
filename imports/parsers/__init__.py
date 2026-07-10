from .catima import parse as parse_catima_csv
from .native_csv import parse as parse_native_csv
from .native_json import parse as parse_native_json

PARSERS = {
    'catima_csv': parse_catima_csv,
    'native_csv': parse_native_csv,
    'native_json': parse_native_json,
}


def get_parser(source_type):
    try:
        return PARSERS[source_type]
    except KeyError:
        raise ValueError(f'Unknown import source type: {source_type!r}')
