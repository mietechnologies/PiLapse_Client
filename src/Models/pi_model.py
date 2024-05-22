class Pi:
    name = ""
    room = {} # contains 'name' and 'windowDirection'
    location = {} # contains 'coordinates' which is an array of two values, latitude and longitude
    model = ""

    def __init__(self, name, room_name, window_direction, longitude, latitude):
        self.name = name
        self.room = { 'name': room_name, 'windowDirection': window_direction }
        self.location = { 'coordinates': [longitude, latitude] }
        self.model = "Raspberry Pi "

    def to_dict(self):
        return {
            'name': self.name,
            'room': self.room,
            'location': self.location,
            'model': self.model
        }