import os

def system_path(to_dir: str, filename: str = None, should_create: bool = True) -> str:
    '''
    Creates a path to the designated directory/file from the user's root
    directory. By default, if the directory does not exist, this method will 
    create it automatically.

    Params:
    to_dir: str
        The path to construct from the user's home directory.
    filename: str | None
        An optional filename to point to.
    should_create: bool
        This parameter tells the method whether you want it to create the path
        if it doesn't exist.

    Returns: str
        The constructed path pointing to the desired directory/file.
    '''
    root = os.path.expanduser('~/')
    directory = os.path.join(root, to_dir)
    if should_create and not os.path.exists(direcotry):
        os.makedirs(directory)

    if filename:
        return os.path.join(directory, filename)

    return directory

def is_file(path: str) -> bool:
    '''
    Determines if the given path is a valid file.

    Params:
    path: str
        The full path to the file/directory to be checked.
    '''
    return os.path.isfile(path)

def project_path(path: str = None, filename: str = None, should_create: bool = True) -> str:
    '''
    
    '''
    root = os.path.dirname(__file__)
    project = os.path.join(root, '..')
    if path is not None:
        directory = os.path.join(project, path)
    else:
        directory = project

    if should_create and not os.path.exists(directory):
        os.makedirs(directory)

    if filename:
        return os.path.join(directory, filename)
    return directory.replace(' ', '\\ ')