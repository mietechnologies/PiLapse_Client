from Utils.mielib import custominput as ci

class Server:
    __project_url = 'https://github.com/mietechnologies/PiLapse_Server'

    url = None
    token = None

    data = {}

    def __init__(self, data):
        if data:
            self.data = data
            self.url = data.get('url', None)
            self.jwt = data.get('token', None)
        else:
            self.build()

    def build(self):
        if ci.bool_input('Do you have the server project running?', True, True):
            address = ci.server_address_input('What is the URL of your server?')
            # TODO: Connect to server, if it passes move on. Otherwise state
            # error and repeat server input step
            # Once URL is confirmed, save it.
            self.url = address

            name = input('What is your first name?')
            # TODO: Ask for the account email address, clarifying an account 
            # will be made if one isn't found
            # TODO: Clarify that if this is a new account, the password will be
            # set to this value; also clarify the password and email address are
            # not saved locally
            password = ci.confirm_input('What is your password?')
            # TODO: Once account is created/accessed save the token

        else:
            print(f'Please download and run the server project: {self.__project_url}')
            print('Once you\'ve done that, please continue.')
            self.build()

