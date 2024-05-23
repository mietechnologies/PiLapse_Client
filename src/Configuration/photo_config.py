from Utils.mielib import custominput as ci
from Utils.mielib.option import Option

class Photo:
    schedule_type = None
    check_schedule = None
    schedule = None
    threshhold = None

    data = {}

    def __init__(self, data):
        if data:
            self.data = data
            self.schedule_type = data.get('schedule_type')
            self.check_schedule = data.get('check_schedule')
            self.schedule = data.get('schedule')
        else:
            self.build()

    def build():
        if ci.bool_input('Would you like to take a photo daily?', True, True):
            if ci.bool_input('Would you like to take only one photo a day?',
             True, True):
                time = ci.time_input('What time each day would you like ' \
                    'the photo taken?')
                # TODO: Set the schedule_type based upon an enum
                # TODO: Construct the schedule
            else:
                if ci.bool_input('Would you like to use significant times ' \
                    'to take the photos? (i.e. sunrise, sunset, solor zenith)'):
                    significant_times = ci.list_input('What time triggers ' \
                        'would you like to take a photo at?', [
                            Option('sr', 'sunrise', 'sunrise'),
                            Option('ss', 'sunset', 'sunset'),
                            Option('sz', 'solar-zenith', 'solar-zenith')
                        ])
                        # TODO: Setup schedule_type and set schedule
                else:
                    # TODO: Gather times the user would like photos taken
                    print('Todo')
        else:
            # TODO: Figure out interval
            print('Todo')

        threshhold = ci.int_input('What do you want to set your time lapse ' \
            'threshhold number at? (The number of photos before a time lapse ' \
            'is made)')
        continue_lopp = ci.bool_input('Would you like the program to continue' \
            ' running with the same schedule once it reached the threshhold?')