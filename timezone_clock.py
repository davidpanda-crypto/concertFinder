"""
Digital Clock Application with Multiple Time Zone Support
Displays current time in different time zones with real-time updates
"""

from flask import Flask, render_template, jsonify
from datetime import datetime
import pytz

app = Flask(__name__)

# Define time zones to display
TIMEZONES = [
    'US/Eastern',
    'US/Central',
    'US/Mountain',
    'US/Pacific',
    'Europe/London',
    'Europe/Paris',
    'Asia/Tokyo',
    'Asia/Hong_Kong',
    'Australia/Sydney',
    'Pacific/Auckland'
]


@app.route('/')
def index():
    """Render the main clock page"""
    return render_template('index.html', timezones=TIMEZONES)


@app.route('/api/time')
def get_time():
    """API endpoint to get current time in all configured time zones"""
    time_data = {}
    
    for tz_name in TIMEZONES:
        try:
            tz = pytz.timezone(tz_name)
            current_time = datetime.now(tz)
            
            # Extract time components
            time_data[tz_name] = {
                'timezone': tz_name,
                'time': current_time.strftime('%H:%M:%S'),
                'date': current_time.strftime('%A, %B %d, %Y'),
                'offset': current_time.strftime('%z'),
                'display_name': current_time.tzname()
            }
        except Exception as e:
            print(f"Error processing timezone {tz_name}: {e}")
    
    return jsonify(time_data)


@app.route('/api/time/<timezone>')
def get_time_for_timezone(timezone):
    """API endpoint to get current time for a specific timezone"""
    try:
        tz = pytz.timezone(timezone)
        current_time = datetime.now(tz)
        
        return jsonify({
            'timezone': timezone,
            'time': current_time.strftime('%H:%M:%S'),
            'date': current_time.strftime('%A, %B %d, %Y'),
            'offset': current_time.strftime('%z'),
            'display_name': current_time.tzname()
        })
    except pytz.exceptions.UnknownTimeZoneError:
        return jsonify({'error': f'Unknown timezone: {timezone}'}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
