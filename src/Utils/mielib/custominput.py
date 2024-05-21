import re
from .option import Option, check_against, construct_output, process_option_list

__url_pattern = r'(https?:\/\/)?(www\.)?[-a-zA-Z0-9@:%._\+~#=]{1,256}\.[a-zA-Z0-9()]{1,6}\b([-a-zA-Z0-9()@:%_\+.~#?&\/\/=]*)'

def int_input(output: str, default: int=None):
    message = "{} ".format(output)
    if default:
        message += "[{}] ".format(default)

    while (True):
        user_input = input(message)
        if default and user_input == "":
            return default
        else:
            try:
                valid_input = int(user_input)
                return valid_input
            except:
                print("I'm sorry, I didn't understand that input.")

def confirm_input(output: str):
    valid_password = False

    while (not valid_password):
        first_password = input(output)
        second_password = input("Confirm your previous input by typing it "\
            "again please:")

        while (second_password != first_password):
            second_password = input("Inputs don't match. Please re-enter" \
                "the original input: (Enter '!' to restart) ")
            if second_password == "!":
                break
        else:
            return first_password

def bool_input(output: str, default=None, abrv=True):
    true_answers = ["y", "yes"]
    false_answers = ["n", "no"]
    ammendment = "y/n" if default is None else "Y/n" if default else "y/N"
    accepted_answers = true_answers + false_answers + ['']

    message = f'{output} [{ammendment}] '
    valid_input = False

    while not valid_input:
        user_response = input(message).lower()

        if user_response in true_answers:
            return True
        if user_response in false_answers:
            return False
        if default and user_response in accepted_answers:
            return default

        message = f'I\'m sorry, I didn\'t understand that input. {output} [{ammendment}] '

def choice_input(output, options, default=None, abrv=True):
    option_list = construct_output(options, default, abrv)
    message = f'{output} {option_list} '
    valid_input = False

    while not valid_input:
        user_input = input(message)
        possible_option = check_against(options, user_input)

        if user_input == "" and default is not None:
            return default.response
        elif possible_option is not None:
            return possible_option.response
        else:
            print("I'm sorry, I didn't understand that input.")

def list_input(output, options):
    option_list = construct_output(options, abrv=False)
    message = f'{output} {option_list}(enter your list separated by \',\') '
    valid_input = False

    while not valid_input:
        user_input = input(message)
        user_list = list(set(process_option_list(user_input)))
        possible_options = []

        for item in user_list:
            possible_option = check_against(options, item)
            if possible_option:
                possible_options.append(possible_option)
            else:
                print("I'm sorry, that is an invalid response. Please try again.")
                possible_options = []
                break
        else:
            return possible_options

def range_input(output, lower, upper, default=None):
    message = f'{output} [{lower}-{upper}] '

    while True:
        try:
            user_input = input(message)

            if default and user_input == "":
                return default

            user_input = int(user_input)

            if user_input in range(lower, upper + 1):
                return user_input
        except:
            print("I'm sorry, I didn't understand that input.")

        print("I'm sorry, that number is out of range, please try again.")

def time_input(output, default=None):
    regex = r'^([012])?\d:[0-5][0-9] ?(p|a|P|a)?(m|M)?$'
    message = "{} ".format(output)
    user_input = ""
    valid_input = False

    while not valid_input:
        user_input = input(message)
        valid_input = re.fullmatch(regex, user_input)
    else:
        return user_input

def url_input(output) -> str:
    ammendment = '[http://www.example.com]'
    message = f'{output} {ammend} '
    user_response = None
    valid_input = False

    while not valid_input:
        user_response = input(message)
        valid_input = re.fullmatch(__url_pattern, user_response)
    else:
        return user_response

def server_address_input(output):
    ammendment = "[192.168.1.1 / www.example.com]"
    message = f'{output} {ammendment} '

    ip_pattern = r'^(?:[0-9]{1,3}\.){3}[0-9]{1,3}$'
    user_response = None
    valid_response = False

    while not valid_response:
        user_response = input(message)
        valid_response = re.fullmatch(ip_pattern, user_response) or re.fullmatch(__url_pattern, user_response)

        if not valid_response:
            print("That server address is invalid, please try again.")
    else:
        return user_response
