# PiLapse Client

## Purpose

This project is the client implementation for the [PiLapse Server](https://github.com/mietechnologies/PiLapse_Server) program.

It is designed to take photos at a set interval (both dynamic and static), process those photos, then send them to the server application. Once the threshold number of photos have been taken and uploaded the server application will then compile those photos into a time lapse video. **At present, the server is required for the full use of this program without modification.**

The original use case for this program was to take photos at the zenith of the sun (for the best lighting) every day on the original Raspberry Pi Zero and a PiCamera Module v2. See the [Technical Details](#technical-details) for more information on the build and usage of this program.

This project was designed, built, and tested by [Brett Chapin](https://github.com/BAChapin). 

## Technical Notes

The **original** build of this program can be found here: [PiCamLapse](https://github.com/BAChapin/PiCamLapse)

That implementation of this program is a standalone piece of software, it was self contained and handle taking, processing, organizing, and storing the photos. It had a rudimentary reporting system which may be broken at present due to the software being very out of date and no longer supported. This was the program that was designed for the Raspberry Pi Zero and PiCamera Module v2. At the time I was unable to get that hardware to generate the time lapse video output. My assumption was due to its lack of horsepower in both RAM and processing.

Due to the limitations of the Raspberry Pi Zero, I had the idea of creating a server application that could handle the heavy lifting of processing the photos into the time lapse video. Which is where the seperation of responsibility came into play.

Now with the Raspberry Pi Zero 2, the device may have the needed performance to create the output video. However, that will be a future implementation (see section below).

For my current setup for **this** project I am using the Raspberry Pi Zero 2 with the PiCamera Module v3. For the server application, I am running that on a Raspberry Pi 4B. Since I am using the newest Camera Module I will be using `picamera2` library.

## Future Implementations

This section will contain all of the future features I want to implement as this software grows. If it has been implemented it will be checked off the list and it will have the commit SHA added at the end of the line.

- [ ] Standalone Capabilities
  - [ ] Completely process photos on device
  - [ ] Once the threshold number of photos is reached, create video time lapse.
- [ ] Short Form Time Lapse Capability (taking photos on shorter increments like 1 second)
- [ ] Standalone Video Server
  - [ ] Constantly Running and Streaming Video
  - [ ] "Publicly" Accessible
  - [ ] Ability to connect with a Mobile app and triggering a photo remotely using the video as a view finder
- [ ] Machine Learning Capabilities
  - [ ] Ability to add custom ML models
  - [ ] Ability to trigger a photo to be taken when certain criteria is met (i.e. a squirrel/person/dog/cat is in the scene)

## Usage

_This program is brought to you by [MieTech LLC](https://github.com/mietechnologies)._