class Option:
    abreviated = ""
    full = ""
    response = None
    all = []

    def __init__(self, abrv, full, response):
        self.abreviated = abrv.lower()
        self.full = full.lower()
        self.response = response
        self.all = [abrv, full]

    def __eq__(self, __o: object) -> bool:
        if (isinstance(__o, Option)):
            return self.all == __o.all

    def contains(self, input):
        return input.lower() in self.all

    def set_default(self):
        self.abreviated = self.abreviated.upper()
        self.full = self.full.upper()

def check_against(options, input):
    for option in options:
        if option.contains(input):
            return option
    else:
        return None

def construct_output(options, default=None, abrv=True):
    choice_list = []

    for option in options:
        if option == default:
            option.set_default()

        if abrv:
            choice_list.append(option.abreviated)
        else:
            choice_list.append(option.full)

    output = "|".join(choice_list)
    return "[{}]".format(output)