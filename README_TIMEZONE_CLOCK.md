# Global Timezone Clock

A digital clock application that displays the current time in different time zones around the world.

## Features

- 🌍 **10 Major Timezones**: Displays time for major cities and regions worldwide
- ⏱️ **Real-time Updates**: Updates every second with smooth animations
- 📱 **Responsive Design**: Works perfectly on desktop, tablet, and mobile devices
- 🎨 **Modern UI**: Beautiful gradient background with card-based layout
- 🔄 **Live Sync**: API-based architecture for easy expansion

## Supported Timezones

- US/Eastern (New York)
- US/Central (Chicago)
- US/Mountain (Denver)
- US/Pacific (Los Angeles)
- Europe/London (London)
- Europe/Paris (Paris)
- Asia/Tokyo (Tokyo)
- Asia/Hong_Kong (Hong Kong)
- Australia/Sydney (Sydney)
- Pacific/Auckland (Auckland)

## Installation

1. Install dependencies:
```bash
pip install -r requirements.txt
```

2. Run the application:
```bash
python timezone_clock.py
```

3. Open your browser and navigate to:
```
http://localhost:5000
```

## API Endpoints

### Get all timezones
```
GET /api/time
```
Returns current time data for all configured timezones.

### Get specific timezone
```
GET /api/time/<timezone>
```
Returns current time data for a specific timezone.

## Usage

The application displays a grid of clock cards, each showing:
- Timezone name and region
- Current time in HH:MM:SS format
- Full date
- UTC offset
- Timezone abbreviation

## Customization

To add or modify timezones, edit the `TIMEZONES` list in `timezone_clock.py`:

```python
TIMEZONES = [
    'US/Eastern',
    'US/Central',
    'US/Mountain',
    'US/Pacific',
    # Add more timezones here
]
```

## Technical Stack

- **Backend**: Python Flask
- **Frontend**: HTML5, CSS3, JavaScript
- **Timezone Handling**: pytz library
- **Styling**: Modern CSS with gradients and animations

## License

MIT License
