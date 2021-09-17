#!/usr/bin/env python3
"""
Used to anonymize real tsv/csv files, keeping mapping between original and anonymized data so that process
can be reversed on optimized files.

Unlike scripts/transform-csv.py which was this based on, file type configuration is hardcoded, so that
users are protected from configuration mistakes.
"""
import collections
import csv
import sys
import zipfile
import random
import re
from pathlib import Path
import codecs
import io
import os.path

from dataclasses import dataclass
from typing import Iterable, Optional, List

from gooey import Gooey, GooeyParser

ENCODED_DIGITS = 16


def random_digits():
    return ''.join(str(random.randint(0, 9)) for _ in range(ENCODED_DIGITS))


class QueueItem:
    def process(self, worker: 'Worker'):
        raise NotImplementedError()

    def output_name(self) -> str:
        raise NotImplementedError()


class EncodeItem(QueueItem):
    def open(self):
        raise NotImplementedError()

    def process(self, worker: 'Worker'):
        with self.open() as source:
            dest = io.StringIO()
            reader, writer = self.config.csv_reader_writer(source, dest)
            encode = worker.encode_value
            mapper = self.config.mapper
            writer.writeheader()
            writer.writerows([mapper(row, encode) for row in reader])
        worker.output_zipfile.writestr(worker.unique_output_name(self.output_name()), dest.getvalue())


@dataclass
class EncodeFile(EncodeItem):
    config: 'FormatConfig'
    path: Path

    def output_name(self) -> str:
        return self.path.name

    def open(self):
        return open(self.path, "r", encoding='utf-8')

    def __str__(self):
        return f'{self.path}) -> {self.output_name()}'


@dataclass
class EncodeFileFromZip(EncodeFile):
    config: 'FormatConfig'
    zip_path: Path
    zip_file: zipfile.ZipFile
    name: str

    def output_name(self) -> str:
        return f'{self.zip_path.name}/{self.name}'

    def open(self):
        return codecs.getreader('utf-8')(self.zip_file.open(self.name))

    def __str__(self):
        return f'{self.zip_path} ({self.name}) -> {self.output_name()}'


class Worker:
    queue: List['QueueItem']
    output_directory: Path
    encoded_mappings: dict[str, str]
    encoded_values: set[str]
    output_zipfile: zipfile.ZipFile
    changed_encoded_values: bool = False
    input_zipfiles: dict[Path, zipfile.ZipFile]

    def __init__(self, output_directory, output_zipname=None):
        self.output_directory = Path(output_directory)
        self.output_directory.mkdir(parents=True, exist_ok=True)
        self.encoded_mappings = {}
        self.encoded_values = set()
        self.processed_count = 0
        self.input_zipfiles = {}
        self.queue = []
        self.output_names = set()
        output_zipname = output_zipname or 'output.zip'  # TODO: timestamped name by default?
        self.output_zipfile = zipfile.ZipFile(self.output_directory / output_zipname, mode="w")

    def unique_output_name(self, name: str):
        if name in self.output_names:
            # TODO
            assert False
        self.output_names.add(name)
        return name

    def find_files(self, paths, for_encode):
        paths = collections.deque(Path(x) for x in paths)
        while paths:
            path = paths.popleft()
            if not path.exists():
                print(f'{path} does not exist, skipping')
            if path.is_dir():
                paths.extend(path.iterdir())
            elif path.suffix.lower().endswith('.zip'):
                self.input_zipfiles[path] = f = zipfile.ZipFile(path)
                for name in f.namelist():
                    if for_encode:
                        config = FormatConfig.get_config(name)
                        if config is not None:
                            self.queue.append(EncodeFileFromZip(config, path, f, name))
                    else:
                        self.queue.append((path, name))
            else:
                if for_encode:
                    config = FormatConfig.get_config(path.name)
                    if config is not None:
                        self.queue.append(EncodeFile(config, path))
                else:
                    self.queue.append((None, path))

    def encode_value(self, value):
        if not value:
            return ''
        try:
            return self.encoded_mappings[value]
        except KeyError:
            while True:
                encoded = 'enc-' + random_digits()
                if encoded not in self.encoded_values:
                    self.encoded_values.add(encoded)
                    break
            self.encoded_mappings[value] = encoded
            self.changed_encoded_values = True
            return encoded

    def process_files(self):
        for queue_item in self.queue:
            print(f'Processing {queue_item}')
            queue_item.process(self)
            self.processed_count += 1
        print(f'Successfully processed {self.processed_count} data files')

    def save_mappings(self):
        # TODO: write to temp and rename?
        with open(self.output_directory / 'mapping.tsv', mode="w", encoding='utf-8') as f:
            writer = csv.writer(f, dialect='excel-tab')
            writer.writerows(self.encoded_mappings.items())

    def load_mappings(self, path):
        with open(path, mode="r", encoding='utf-8') as f:
            reader = csv.reader(f, dialect='excel-tab')
            self.encoded_mappings = dict(reader)
            self.encoded_values = set(self.encoded_mappings.values())

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.output_zipfile.close()
        for f in self.input_zipfiles.values():
            f.close()
        self.save_mappings()


@dataclass
class FormatConfig:
    carrier: str
    dialect: str
    file_mask: str
    clear_columns: Iterable[str]
    encode_columns: Iterable[str]
    delimiter: Optional[str] = None

    def matches(self, filename, flags=0):
        return re.match(self.file_mask, filename, flags=flags)

    def csv_reader_writer(self, source, dest):
        config = {'dialect': self.dialect}
        if self.delimiter:
            config['delimiter'] = self.delimiter
        reader = csv.DictReader(f=source, **config)
        writer = csv.DictWriter(f=dest, fieldnames=reader.fieldnames, **config)
        return reader, writer

    def mapper(self, d, encode):
        d = d.copy()
        for key in self.clear_columns:
            d[key] = ''
        for key in self.encode_columns:
            d[key] = encode(d.get(key) or '')
        return d

    @classmethod
    def get_config(cls, filename):
        return next((config for config in cls.CONFIGS if config.matches(filename)), None)

    @classmethod
    def get_config_descriptions(cls):
        carriers = collections.defaultdict(list)
        for config in cls.CONFIGS:
            yield {
                'type': 'MessageDialog',
                'menuTitle': f'{config.carrier} - {config.file_mask}',
                'caption': f'Configuration for {config.carrier} - {config.file_mask}',
                'message': f'Clear columns: {config.clear_columns or "None"}\n'
                           f'Encode columns: {config.encode_columns or "None"}'
            }


FormatConfig.CONFIGS = [
    # AT&T
    FormatConfig(carrier='AT&T', dialect='excel-tab', delimiter='|', file_mask='rawdataoutput',
                 clear_columns={'Number Called To/From'},
                 encode_columns={'Foundation Account Name', 'Billing Account Name', 'Wireless Number'}
                 ),
    # Verizon
    FormatConfig(carrier='Verizon', dialect='excel-tab', file_mask='Wireless Usage Detail',
                 clear_columns={'ECPD Profile ID'},
                 encode_columns={'Wireless Number', 'Account Number', 'User Name', 'Invoice Number', 'Number'}),
    FormatConfig(carrier='Verizon', dialect='excel-tab', file_mask='Acct & Wireless Charges Detail Summary Usage',
                 clear_columns={'ECPD Profile ID', 'Vendor Name / Contact Information'},
                 encode_columns={'Wireless Number', 'Account Number', 'User Name', 'Invoice Number'}),
    FormatConfig(carrier='Verizon', dialect='excel-tab', file_mask='AccountSummary',
                 clear_columns={'ECPD Profile ID', 'Bill Name', 'Remittance Address'},
                 encode_columns={'Account Number', 'Invoice Number'}),
    FormatConfig(carrier='Verizon', dialect='excel-tab', file_mask='Account & Wireless Summary',
                 clear_columns={'ECPD Profile ID'},
                 encode_columns={'Wireless Number', 'Account Number', 'User Name', 'Invoice Number'}),
]

MENU = [
    {'name': 'Help', 'items': [
        {
            'type': 'AboutDialog',
            'menuTitle': 'About',
            'name': 'Byte Analytics Data Anonymizer',
            'description': 'Program to anonymize data files for Byte Analytics Mobile Optimizer',
            'version': 'latest',  # TODO: git tag
            'copyright': '2021',
            'website': 'https://github.com/reef-technologies/byteanalytics-anonymizer',
            'developer': 'https://reef.pl/',
            'license': 'GPL v3'
        },
        {
            'type': 'Link',
            'menuTitle': 'Check for updates',
            'url': 'https://github.com/reef-technologies/byteanalytics-anonymizer/releases',
        }
    ]},
    {'name': 'Carrier Configuration', 'items': list(FormatConfig.get_config_descriptions())},

]


def add_common_arguments(parser):
    parser.add_argument('--input_paths', nargs='+', metavar='Input files', widget='MultiFileChooser',
                        help='Files or directories to be processed', required=True)
    parser.add_argument('--output_directory', metavar='Output directory', widget='DirChooser',
                        help='Path to store output files', required=True)


def main():
    parser = GooeyParser(
        description='Program to anonymize data files for Byte Analytics Mobile Optimizer',
        epilog='Run without arguments to launch the GUI',
    )

    subparsers = parser.add_subparsers(dest='action', required=True)
    encode = subparsers.add_parser('Encode', help='Anonymize the data files')
    add_common_arguments(encode)

    decode = subparsers.add_parser('Decode', help='De-anonymize the data files')
    add_common_arguments(decode)
    decode.add_argument('--mapping_file', metavar='Mapping file', widget='FileChooser', required=True,
                        help='mapping.tsv file', gooey_options={'wildcard': "Tab separated file (*.tsv)|*.tsv|"})

    args = parser.parse_args()
    if args.action == 'Encode':
        for_encode = True
    elif args.action == 'Decode':
        for_encode = False
    else:
        assert False

    with Worker(args.output_directory) as worker:
        worker.find_files(args.input_paths, for_encode=for_encode)
        if for_encode and (path := Path(args.output_directory) / 'mapping.tsv').exists():
            worker.load_mappings(path)
        elif not for_encode:
            worker.load_mappings(args.mapping_file)
        worker.process_files()


def get_resource_path(*args):
    if getattr(sys, 'frozen', False):
        # MEIPASS explanation:
        # https://pythonhosted.org/PyInstaller/#run-time-operation
        resource_dir = getattr(sys, '_MEIPASS', None)
    else:
        resource_dir = os.path.normpath(os.path.dirname(__file__))
    return os.path.join(resource_dir, *args)


if __name__ == '__main__':
    if len(sys.argv) > 1:
        # CLI
        IGNORE_COMMAND = '--ignore-gooey'
        if IGNORE_COMMAND in sys.argv:
            sys.argv.remove(IGNORE_COMMAND)
        main()
    else:
        # GUI
        Gooey(
            f=main,
            language='english',
            show_sidebar=True,
            program_name='Byte Analytics Data Encoder',
            advanced=True,
            default_size=(900, 600),
            required_cols=1,
            optional_cols=1,
            menu=MENU,
            image_dir=get_resource_path('images'),
            language_dir=get_resource_path('gooey', 'languages'),
        )()
