# User stories: sat.aljazeera.net (Al Jazeera satellite frequency finder)

DRAFT written on 2026-10-24 from observing the live site (English `/en`, Arabic `/ar`). It describes what the site does
today, so a person who knows the intended behaviour should read it and correct anything that is wrong or missing. Feed it
to the pipeline with `python -m website_test_pipeline.cli intents docs/stories/sat-aljazeera-net.md`, or upload it with a run.

Out of scope on purpose: actually submitting the subscription form (it needs a CAPTCHA and creates a real subscription).

## Find frequencies

- US-01: A visitor opens the home page and sees a finder that asks for their location and a channel.
  - The page title is "Satellite Frequencies" and the finder shows "Select your location" and "Select your channel".
- US-02: A visitor picks a country from a list of countries and the channels available for that country become choosable.
  - The country list starts with "Please select a country" and has about 250 countries (for example Afghanistan, Egypt).
  - The channel picker offers Al Jazeera 2 Channel, Al Jazeera 2 HD Channel, Aljazeera Arabic Channel HD, Al Jazeera Mubasher Channel HD, Al Jazeera Documentary Channel HD and Aljazeera English Channel HD.
- US-03: A visitor picks one or more channels, presses Search, and sees a table of frequencies for that country and those channels.
  - The page address becomes `/en/frequency-search/<country>/<channels>`.
  - The table has the columns SATELLITE, CHANNEL, FREQUENCY, ORBIT, SYMBOL RATE, FEC, POLARITY and TRANSMISSION.
  - Each row names a satellite (for example Hotbird 13 G & F, Es'hailSat 1) with a frequency and orbit.
- US-04: Searching a different country gives different results.
  - Egypt and Afghanistan do not return the same satellites.
- US-05: A visitor can open the finder on its own page, "Satellite Frequencies" at `/en/frequency-search`, and get the same choices.

## Map

- US-06: A visitor presses "Go to Map" and the interactive map page opens.
  - The address becomes `/en/map` and the page shows a map.
- US-07: A visitor on the finder page can use the "Use our interactive map to find Al Jazeera" link to reach the same map page.

## Get updates

- US-08: A visitor presses "Subscribe Now" and a subscription form opens at `/en/subscribe`.
  - The form asks for Name, Email, Country, City, Channel, Mobile and Language, and for the code shown in an image.
- US-09: A visitor who presses "Submit Request" with nothing filled in is told which fields are required.
  - A message appears saying fields such as Name, Email, Country, City and Channel are required, and the visitor stays on the form.
- US-10: A visitor can see "New Frequencies" on `/en/newfrequencies`, with a "Subscribe Now" button that leads to the subscription form.

## Tune a receiver

- US-11: A visitor opens "Tune your Receiver" and is guided step by step.
  - The first step says to press the Menu button on the remote control.
  - Pressing "Next" moves to the following step.

## Arabic version

- US-12: A visitor can use the same finder in Arabic at `/ar`, laid out right-to-left.
  - The page title is "الرئيسية | ترددات القنوات الفضائية" and the finder says "أختر موقعك" and "أختر القناة".
- US-13: In Arabic, choosing a country and a channel and pressing "بحث" shows the frequencies table in Arabic.
- US-14: In Arabic, "الذهاب الى الخريطة" opens `/ar/map` and "أشترك اﻷن" opens `/ar/subscribe`.

## Quality

- US-15: Every page loads without an error page, and the links in the page lead to working pages.
- US-16: A cookie / privacy preference notice is offered to the visitor.
