# Nearby Items

The **Nearby** widget does a one-shot location check when Inventory loads: if a shop near you matches one of your item issuers, it surfaces in a "Nearby" card (e.g. walk into Tesco and see your Tesco gift card without searching for it). It uses OpenStreetMap's Overpass API — no API key needed, no data sent to a paid geolocation service, and your coordinates are used for that one lookup only, never stored.

## Enabling it site-wide

This is a two-step opt-in:

1. **Site Settings → Nearby Items** → tick **Allow the Nearby widget to query OpenStreetMap**. This is the master switch; if it's off, the widget doesn't run for anyone regardless of their own preference.
2. Each user then enables it individually on their own **Preferences** page (**Enable Nearby widget**). It's opt-in per-user because it uses the browser's geolocation permission.

## Overpass API URL

Defaults to the public `overpass-api.de` instance, which is free but rate-limited under heavy use. If you self-host an Overpass instance (see the [official install guide](https://wiki.openstreetmap.org/wiki/Overpass_API/Installation)), point this field at it instead to avoid the public instance's limits entirely.

## Privacy

The widget only ever sends the browser's coarse location to the configured Overpass endpoint to search for nearby points of interest matching your items' issuer names — it never sends your item data, account details, or precise location history anywhere. Location is requested fresh each time the widget loads and isn't stored server-side.

## Troubleshooting

- **Widget doesn't appear**: check both the site-wide switch above and the user's own Preferences toggle.
- **"Location permission denied"**: the browser blocked the geolocation prompt — re-enable it in the browser's site settings for this domain.
- **No nearby results**: OpenStreetMap coverage varies by region and depends on the issuer being mapped as a matching point of interest near you.
