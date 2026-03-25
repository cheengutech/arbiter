import asyncio
import os
from dotenv import load_dotenv
from twilio.rest import Client
import BAC0
import sys
sys.stdout.reconfigure(reconfigure=True)

# Load credentials from .env file
load_dotenv('/home/jwpark05/.env')

# Twilio setup
client = Client(os.getenv('TWILIO_ACCOUNT_SID'), os.getenv('TWILIO_AUTH_TOKEN'))

def send_sms(message):
    client.messages.create(
        body=message,
        from_=os.getenv('TWILIO_PHONE_NUMBER'),
        to=os.getenv('MY_PHONE_NUMBER')
    )
    print(f'SMS sent: {message}')

async def main():
    # Track previous alarm states to avoid repeat alerts
    alarm_states = {
        'teh2_humidity': 'normal',
        'teh1_humidity': 'normal',
    }

    async with BAC0.start(ip='10.10.60.50/24') as bacnet:
        await asyncio.sleep(5)
        await bacnet._discover()
        await asyncio.sleep(30)
        print('Arbiter online. Polling every 5 minutes.')

        while True:
            try:
                # Read TEH-2 humidity and temperature
                teh2_hum = await bacnet.read('11:1 analogInput 1 presentValue')
                teh2_temp = await bacnet.read('11:1 analogInput 2 presentValue')

                # Read TEH-1 humidity and temperature
                teh1_hum = await bacnet.read('11:1 analogInput 3 presentValue')
                teh1_temp = await bacnet.read('11:1 analogInput 4 presentValue')

                print(f'TEH-2: {teh2_hum:.1f}% RH | {teh2_temp:.1f}F')
                print(f'TEH-1: {teh1_hum:.1f}% RH | {teh1_temp:.1f}F')

                # Check TEH-2 humidity
                if teh2_hum > 55 and alarm_states['teh2_humidity'] == 'normal':
                    send_sms(f'ARBITER: TEH-2 Humidity HIGH {teh2_hum:.1f}% at Legion of Honor')
                    alarm_states['teh2_humidity'] = 'high'
                elif teh2_hum < 40 and alarm_states['teh2_humidity'] == 'normal':
                    send_sms(f'ARBITER: TEH-2 Humidity LOW {teh2_hum:.1f}% at Legion of Honor')
                    alarm_states['teh2_humidity'] = 'low'
                elif 40 <= teh2_hum <= 55 and alarm_states['teh2_humidity'] != 'normal':
                    send_sms(f'ARBITER: TEH-2 Humidity returned to normal {teh2_hum:.1f}%')
                    alarm_states['teh2_humidity'] = 'normal'

                # Check TEH-1 humidity
                if teh1_hum > 55 and alarm_states['teh1_humidity'] == 'normal':
                    send_sms(f'ARBITER: TEH-1 Humidity HIGH {teh1_hum:.1f}% at Legion of Honor')
                    alarm_states['teh1_humidity'] = 'high'
                elif teh1_hum < 40 and alarm_states['teh1_humidity'] == 'normal':
                    send_sms(f'ARBITER: TEH-1 Humidity LOW {teh1_hum:.1f}% at Legion of Honor')
                    alarm_states['teh1_humidity'] = 'low'
                elif 40 <= teh1_hum <= 55 and alarm_states['teh1_humidity'] != 'normal':
                    send_sms(f'ARBITER: TEH-1 Humidity returned to normal {teh1_hum:.1f}%')
                    alarm_states['teh1_humidity'] = 'normal'

            except Exception as e:
                print(f'Poll error: {e}')

            # Wait 5 minutes before next poll
            await asyncio.sleep(300)

asyncio.run(main())
