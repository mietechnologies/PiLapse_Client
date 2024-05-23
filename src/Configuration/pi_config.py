from Utils.mielib import custominput as ci
from Models.pi_model import Pi as Model

class Pi:
    id = None

    data = {}

    def __init__(self, data):
        if data:
            self.data = data
            self.id = data.get('id', None)
        else:
            self.build()

    def build(self):
        name = input('What would you ike to name this client? ')
        room = input('What room is this Pi placed in? ')
        window_direction = input('What direction is the Pi facing? ')
        long = input('What is the Pi\'s longitude? ')
        lat = input('What is the Pi\'s latitude? ')
        # TODO: Collect the clients model information to construct the model
        model = Model(name, room, window_direction, long, lat)
        # TODO: Connect to server to add this Pi, save the returned ID