# Open-Meteo weather provider notice

The optional desktop-companion weather feature uses the Open-Meteo Forecast API at `https://api.open-meteo.com`.

- Weather data is provided by Open-Meteo under CC BY 4.0. Source and attribution: https://open-meteo.com/
- Terms of service: https://open-meteo.com/en/terms
- Privacy policy: https://open-meteo.com/en/terms#privacy
- Open-Meteo states that API-server logs can include submitted geographic coordinates and are deleted after 90 days.
- The free public API is intended for non-commercial use and is subject to provider rate limits. A commercial deployment needs an appropriate Open-Meteo plan or a replacement provider review.

This integration is disabled by default. It sends only the coordinates explicitly entered by the user after the user accepts the provider notice. It does not infer a location from IP address, system locale, profile data, or device location. The feature can be disabled at any time in Companion Center → Privacy and sensors; disabling stops future provider requests and clears the weather projection while retaining the entered settings for a later opt-in.
