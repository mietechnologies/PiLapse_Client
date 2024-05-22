import requests

class Service:
    base_url = ""
    jwt = None
    headers = {}

    def __init__(self, server_url, user_token):
        self.base_url = server_url
        self.jwt = user_token
        self.headers = {
            'Authorization': f'Bearer {user_token}',
            'Content-Type': 'application/json'
        }

    def confirm_connect(self) -> bool:
        # TODO: Logic to check to see if a connection to the server can be made
        return False

    def create_account(self) -> str:
        constructed_url = f'{self.base_url}/user/signup'
        # TODO: Logic for creating an account, needs an Account object
        # Returns JWT from response
        return ""

    def login(self) -> str:
        constructed_url = f'{self.base_url}/user/login'
        # TODO: Logic for signing a user in, needs 'email' and 'password'
        # Returns JWT from response
        return ""

    def add_pi(self, pi) -> str:
        constructed_url = f'{self.base_url}/pi/add'
        # TODO: Logic for adding this Pi to the server
        # Needs JWT
        # Returns the Pi's identifier
        return ""

    def send_photo(self, pi_id, photo):
        constructed_url = f'{self.base_url}/pi/{pi_id}/photos'
        # TODO: Logic for a POST request, sending the photo for THIS pi's id
        # Needs JWT


    # Collect JWT
    # headers = { 'Authorization': f'Bearer {jwt}', 'Content-Type': 'application/json'}
    # make post: response = requests.post(url, headers=headers, json=data)
    # Check response: response.status_code