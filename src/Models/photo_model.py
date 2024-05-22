import base64

class Photo:
    image = None
    temperature = None
    date = ""

    def __init__(self, image_path, temperature, date):
        self.temperature = temperature
        self.date = date
        with open(image_path, 'r') as file:
            contents = file.read()
            self.image = base64.b64encode(contents).decode('utf-8')

    def to_dict(self):
        return {
            'image': self.image,
            'temperature': self.temperature,
            'date': self.date
        }