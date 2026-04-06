import hashlib
import json
import urllib.parse
import urllib.request

# Settings
CACHE_DIR = "lrc_cache/"

# Store lrc file to the specified directory
def store_lrc_to_cache(title, artist, album, length, lyrics):
  # Don't cache empty lyrics files
  if not lyrics:
      return
  filename = f"{title} {artist} {hashlib.sha256((title + artist + album + str(length)).encode()).hexdigest()}.lrc"
  with open(CACHE_DIR + filename, "w") as file:
    file.write(lyrics)

# Get a lrc file from the cache directory, return None if lyrics haven't been cached yet
def get_lrc_from_cache(title, artist, album, length):
  filename = f"{title} {artist} {hashlib.sha256((title + artist + album + str(length)).encode()).hexdigest()}.lrc"
  try:
    with open(CACHE_DIR + filename, "r") as file:
      return file.read()
  except:
    return None

# Get lyrics in the .lrc format from lrclib or the local cache
def get_lyrics(title, artist, album, length):
  length = str(int(length) // 1000000) # Convert from microseconds to seconds

  # Check if we have the file in our cache
  cached_lyrics = get_lrc_from_cache(title, artist, album, length)
  if cached_lyrics:
    return cached_lyrics

  # Try hitting the lrclib API if they have synced lyrics
  url = f"https://lrclib.net/api/get?artist_name={urllib.parse.quote(artist)}&track_name={urllib.parse.quote(title)}&album_name={urllib.parse.quote(album)}&duration={urllib.parse.quote(str(length))}"
  json_response = {"syncedLyrics": ""}
  try:
    request = urllib.request.Request(url, headers={"User-Agent": "mediaplayer/1.0"})
    with urllib.request.urlopen(request, timeout=2) as response:
      json_response = json.loads(response.read().decode())
  except Exception as err: # Lookup unsuccessful
    return f"LRCERROR: {err}"

  # Store lrc file to our cache and return them
  store_lrc_to_cache(title, artist, album, length, json_response["syncedLyrics"])
  return json_response["syncedLyrics"]

# Convert a lrc file to our bespoke format, which is a list of lists
# The first element in the nested list is the timestamp, with the second being the line
def convert_lrc(lyrics):
  lyrics = lyrics.split("\n")
  lyrics_list = []
  for line in lyrics:
    timestamps_seconds = []
    while "]" in line:
      timestamp, line = line.split("]", 1)

      minutes = int(timestamp[1] + timestamp[2])
      seconds = int(timestamp[4] + timestamp[5]) + int(timestamp[7] + timestamp[8]) / 100

      timestamps_seconds.append(minutes * 60 + seconds)

    for seconds in timestamps_seconds:
      lyrics_list.append((seconds, line))

  return sorted(lyrics_list, key=lambda x: x[0])

# Use binary search to get the current line
def get_current_line(lyrics, time):
  lower_bound = 0
  upper_bound = len(lyrics) - 1

  iterations = 0
  while lower_bound <= upper_bound:
    midpoint = (lower_bound + upper_bound) // 2
    if iterations >= 30:
      return lyrics[midpoint]
    if lyrics[midpoint][0] < time:
      lower_bound = midpoint
    elif lyrics[midpoint][0] > time:
      upper_bound = midpoint
    else:
      return lyrics[midpoint]
    iterations += 1
  return lyrics[0]
