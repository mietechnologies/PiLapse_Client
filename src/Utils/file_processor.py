import yaml
from Utils.path import is_file

def parse_yaml(path: str) -> dict:
    '''
    Parses a yaml file at a given path.

    Params:
    path: str
        The absolute path to the yaml file to be parsed.

    Returns: dict
        Returns a dictionary of the keys and values parsed from the yaml file or
        None if the file is invalid.
    '''
    if is_file(path):
        with open(path, 'r', encoding='utf8') as yaml_file:
            return yaml.load(yaml_file, yaml.Loader)
    return None

def write_yaml(path: str, data: dict) -> dict:
    '''
    Converts a dictionary into a yaml file and saves the data to a given file
    path.

    Params:
    path: str
        The absolute path for where to write the yaml file.
    data: dict
        The data to write to the yaml file.

    Returns: dict
        The data that was written to the yaml file. The data returned is the
        data read from the file that was saved for the purpose of confirmation
        that it was written as expected.
    '''
    with open(path, 'w', encoding='utf8') as yaml_file:
        yaml.dump(data, yaml_file, default_flow_style=False)
        return parse_yaml(path)