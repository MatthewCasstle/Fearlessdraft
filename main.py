import os
from flask import Flask, render_template, jsonify, request, url_for, send_from_directory, redirect, make_response
from flask_socketio import SocketIO, emit, join_room, leave_room
import json
import random
import uuid
from google.cloud import storage
import logging
import time
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from collections import defaultdict
import psutil
import threading




logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__, static_folder='static', static_url_path='/static')
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'your_secret_key')
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 31536000  # 1 year in seconds
socketio = SocketIO(app, async_mode='gevent', cors_allowed_origins="*", logger=True, engineio_logger=True)

@app.route('/static/<path:filename>')
def serve_static(filename):
    root_dir = os.path.dirname(os.getcwd())
    return send_from_directory(os.path.join(root_dir, 'static'), filename, max_age=31536000)



# Initialize Google Cloud Storage client
storage_client = storage.Client()
bucket_name = os.environ.get('BUCKET_NAME', 'triple-odyssey-435000-i0.appspot.com')
bucket = storage_client.bucket(bucket_name)

champion_roles_path = 'data/champion_roles.json'

# Load champion data
champion_names_path = 'data/champion_names.json'

def load_champion_roles():
    blob = bucket.blob(champion_roles_path)
    content = blob.download_as_text()
    return json.loads(content)



def log_server_stats():
    cpu_percent = psutil.cpu_percent()
    memory_percent = psutil.virtual_memory().percent
    app.logger.info(f"CPU: {cpu_percent}%, Memory: {memory_percent}%")

champion_roles = load_champion_roles()

def load_champion_names():
    blob = bucket.blob(champion_names_path)
    content = blob.download_as_text()
    return json.loads(content)

champion_names = load_champion_names()

STATIC_URL = '/static'

champion_data = {}

for champion in champion_names:
    champion_data[champion] = {
        'name': champion,
        'icon': f"{STATIC_URL}/champion_images/{champion}.png",
        'splash': f"{STATIC_URL}/champion_splashes/{champion}_splash.jpg",
        'roles': champion_roles.get(champion, [])
    }

# Draft state
drafts = {}

def serialize_draft(draft):
    serialized = draft.copy()
    serialized['used_champions'] = list(draft['used_champions'])
    serialized['fearless_bans'] = list(draft['fearless_bans'])
    serialized['side_swap_requested'] = draft['side_swap_requested']
    return serialized

def deserialize_draft(draft_data):
    deserialized = draft_data.copy()
    deserialized['used_champions'] = set(draft_data['used_champions'])
    deserialized['fearless_bans'] = set(draft_data['fearless_bans'])
    return deserialized

def save_draft_data(room_id, draft_data):
    try:
        blob = bucket.blob(f'drafts/{room_id}.json')
        blob.upload_from_string(json.dumps(draft_data), content_type='application/json')
    except Exception as e:
        logging.error(f"Failed to save draft data to cloud storage: {str(e)}")
        save_draft_data_locally(room_id, draft_data)

def save_draft_data_locally(room_id, draft_data):
    os.makedirs('local_drafts', exist_ok=True)
    with open(f'local_drafts/{room_id}.json', 'w') as f:
        json.dump(draft_data, f)

def load_draft_data(room_id):
    try:
        blob = bucket.blob(f'drafts/{room_id}.json')
        if blob.exists():
            return json.loads(blob.download_as_string())
    except Exception as e:
        logging.error(f"Failed to load draft data from cloud storage: {str(e)}")
    
    return load_draft_data_locally(room_id)

def load_draft_data_locally(room_id):
    try:
        with open(f'local_drafts/{room_id}.json', 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        return None

#@app.before_request
#def log_request_info():
    #app.logger.info('Headers: %s', request.headers)
    #app.logger.info('Body: %s', request.get_data())
#log_server_stats()
from collections import defaultdict
import time

request_counts = defaultdict(lambda: {'count': 0, 'last_request': 0})

@app.before_request
def track_requests():
    ip = request.remote_addr
    current_time = time.time()
    if current_time - request_counts[ip]['last_request'] > 3600:  
        request_counts[ip]['count'] = 0
    request_counts[ip]['count'] += 1
    request_counts[ip]['last_request'] = current_time
    if request_counts[ip]['count'] > 1000:  
        app.logger.warning(f"Possible DDoS from IP: {ip}")

@app.route('/')
def home():
    return render_template('home.html')

@app.route('/draft_settings')
def draft_settings():
    return render_template('draft_settings.html')

@app.route('/create_draft', methods=['POST'])
def create_draft():
    mode = request.form['mode']
    pick_time = int(request.form['pick_time'])
    ban_time = int(request.form['ban_time'])
    games = int(request.form['games']) if mode == 'fearless' else 1
    blue_team_name = request.form['blue_team_name']
    red_team_name = request.form['red_team_name']

    room_id = str(uuid.uuid4())
    draft_order = get_draft_order(1)
    drafts[room_id] = {
        'mode': mode,
        'games': games,
        'current_game': 1,
        'pick_time': pick_time,
        'ban_time': ban_time,
        'blue_team': {'name': blue_team_name, 'picks': [], 'bans': [], 'ready': False},
        'red_team': {'name': red_team_name, 'picks': [], 'bans': [], 'ready': False},
        'current_phase': 'Ready',
        'current_team': draft_order[0],
        'draft_index': -1,
        'draft_order': draft_order,
        'time_left': None,
        'timer_start': None,
        'remaining_champions': champion_names.copy(),
        'used_champions': set(),
        'fearless_bans': set(),
        'hovered_champion': {'Blue': None, 'Red': None},
        'side_swap_requested': {'Blue': False, 'Red': False},
        'previous_drafts': []
    }

    # Save draft data
    draft_data = serialize_draft(drafts[room_id])
    save_draft_data(room_id, draft_data)

    return render_template('create_draft.html', room_id=room_id, blue_team_name=blue_team_name, red_team_name=red_team_name)

def get_draft_order(game_number):
    base_order = ['Blue', 'Red', 'Blue', 'Red', 'Blue', 'Red', 'Blue', 'Red', 'Red', 'Blue', 'Blue', 'Red', 'Red', 'Blue', 'Red', 'Blue', 'Red', 'Blue', 'Blue', 'Red']
    
    if game_number == 5:
        draft_order = base_order[6:12] + base_order[16:]
    elif game_number == 4:
        draft_order = base_order[:12] + base_order[16:]
    else:
        draft_order = base_order
    
    return draft_order


@app.route('/join/<room_id>/<role>')
def join_draft(room_id, role):
    if room_id not in drafts:
        draft_data = load_draft_data(room_id)
        if draft_data:
            drafts[room_id] = deserialize_draft(draft_data)
        else:
            return "Invalid room ID", 404
    return render_template('index.html', room_id=room_id, role=role, champions=champion_data)

@socketio.on('join')
def on_join(data):
    username = data['username']
    room = data['room']
    role = data.get('role', 'spectator')
    join_room(room)
    logging.info(f"User {username} joined room {room} as {role}. Current draft state: {drafts[room]}")
    emit('status', {'msg': f'{username} has entered the draft as {role}.'}, room=room)
    emit('update_draft', serialize_draft(drafts[room]), room=room)

@socketio.on('ready')
def ready_up(data):
    logging.info(f"Received ready event: {data}")
    room = data['room']
    team_name = data['team']
    
    draft = drafts[room]
    if draft['blue_team']['name'] == team_name:
        draft['blue_team']['ready'] = True
        logging.info(f"Blue team ({team_name}) is ready in room {room}")
    elif draft['red_team']['name'] == team_name:
        draft['red_team']['ready'] = True
        logging.info(f"Red team ({team_name}) is ready in room {room}")
    else:
        logging.error(f"Invalid team name received: {team_name}")
        return
    
    if draft['blue_team']['ready'] and draft['red_team']['ready']:
        logging.info(f"Both teams are ready in room {room}. Starting draft.")
        start_draft(room)
    else:
        logging.info(f"Updating draft state in room {room}: Blue ready: {draft['blue_team']['ready']}, Red ready: {draft['red_team']['ready']}")
        emit('update_draft', serialize_draft(draft), room=room)
def start_draft(room):
    logging.info(f"Starting draft for room: {room}")
    draft = drafts[room]
    draft['current_phase'] = 'Ban'
    draft['time_left'] = 30
    draft['timer_start'] = time.time()  
    draft['blue_team']['picks'] = []
    draft['red_team']['picks'] = []
    draft['blue_team']['bans'] = []
    draft['red_team']['bans'] = []
    draft['hovered_champion'] = {'Blue': None, 'Red': None}
    draft['side_swap_requested'] = {'Blue': False, 'Red': False}
    next_turn(room)
    emit('clear_draft', room=room)
    emit('update_draft', serialize_draft(draft), room=room)

def next_turn(room):
    logging.info(f"Next turn for room: {room}")
    draft = drafts[room]
    draft['draft_index'] += 1
    if draft['draft_index'] < len(draft['draft_order']):
        draft['current_team'] = draft['draft_order'][draft['draft_index']]
        if draft['current_game'] < 4:
            if draft['draft_index'] < 6 or (draft['draft_index'] >= 12 and draft['draft_index'] < 16):
                draft['current_phase'] = 'Ban'
            else:
                draft['current_phase'] = 'Pick'
        elif draft['current_game'] == 4:
            if draft['draft_index'] < 6:
                draft['current_phase'] = 'Ban'
            else:
                draft['current_phase'] = 'Pick'
        else:  # Game 5
            draft['current_phase'] = 'Pick'
        draft['time_left'] = 30  
        draft['timer_start'] = time.time()  
    else:
        draft['current_phase'] = 'Complete'
        draft['time_left'] = None  
    logging.info(f"Updated draft state for room {room}: {draft}")
    emit('update_draft', serialize_draft(draft), room=room, broadcast=True)

@socketio.on('update_timer')
def update_timer(data):
    room = data['room']
    draft = drafts[room]
    if draft['current_phase'] not in ['Ready', 'Complete'] and draft['time_left'] is not None:
        elapsed_time = int(time.time() - draft['timer_start'])
        if elapsed_time <= 30:
            new_time_left = 30 - elapsed_time
            if new_time_left != draft['time_left']:
                draft['time_left'] = new_time_left
                emit('timer_update', {'time_left': new_time_left}, room=room)
        elif elapsed_time <= 31:  # 1-second grace period
            draft['time_left'] = 0
            emit('timer_update', {'time_left': 0}, room=room)
        else:
            draft['time_left'] = 0
            auto_lock_in(room)
        


@socketio.on('hover_champion')
def hover_champion(data):
    room = data['room']
    champion = data['champion']
    team = data['team'].capitalize()  
    user_role = data['role'].capitalize()  
    
    logging.info(f"Hover attempt: Room {room}, Champion {champion}, Team {team}, User Role {user_role}")
    
    draft = drafts[room]
    if draft['current_phase'] in ['Ban', 'Pick'] and draft['current_team'] == team:
        draft['hovered_champion'][team] = champion
        logging.info(f"Champion {champion} hovered by {team} in room {room}")
        
        hover_data = {
            'team': team,
            'champion': champion,
            'phase': draft['current_phase'],
            'draft_index': draft['draft_index'],
            'position': get_hover_position(draft, team)  
        }
        
        emit('update_draft', serialize_draft(draft), room=room, broadcast=True)
        emit('champion_hover', hover_data, room=room, broadcast=True)
    else:
        logging.warning(f"Hover attempt failed: Phase {draft['current_phase']}, Current Team {draft['current_team']}")

def get_hover_position(draft, team):
    
    if draft['current_phase'] == 'Ban':
        return len(draft[f'{team.lower()}_team']['bans'])
    else:  # Pick phase
        return len(draft[f'{team.lower()}_team']['picks'])

@socketio.on('lock_in_champion')
def lock_in_champion(data):
    room = data['room']
    champion = data['champion']
    user_role = data['role']
    draft = drafts[room]
    current_team = draft['current_team']
    
    logging.info(f"Lock-in attempt: Room {room}, User role {user_role}, Current team {current_team}, Draft index {draft['draft_index']}, Current phase {draft['current_phase']}")
    
    
    team_name = draft['blue_team']['name'] if user_role == 'blue' else draft['red_team']['name']
    
    if team_name != draft[f"{current_team.lower()}_team"]['name']:
        logging.error(f"Invalid lock-in attempt: User team {team_name} doesn't match current team {draft[f'{current_team.lower()}_team']['name']}")
        return
    
    if champion and champion in draft['remaining_champions']:
        if draft['current_phase'] == 'Ban':
            draft[f"{current_team.lower()}_team"]['bans'].append(champion)
            draft['remaining_champions'].remove(champion)
        elif draft['current_phase'] == 'Pick':
            draft[f"{current_team.lower()}_team"]['picks'].append(champion)
            draft['fearless_bans'].add(champion)
            draft['remaining_champions'].remove(champion)
        
        draft['used_champions'].add(champion)
        draft['hovered_champion'][current_team] = None
        
        emit('update_draft', serialize_draft(draft), room=room, broadcast=True)
        
        if all_picks_complete(draft):
            if draft['mode'] == 'fearless' and draft['current_game'] < draft['games']:
                start_next_game(room)
            else:
                end_draft(room)
        else:
            next_turn(room)
    
    logging.info(f"Updated draft state after lock-in: {draft}")

    # Update draft data
    draft_data = serialize_draft(draft)
    save_draft_data(room, draft_data)

#log_server_stats()

def auto_lock_in(room):
    draft = drafts[room]
    if draft['hovered_champion'][draft['current_team']]:
        lock_in_champion({'room': room, 'champion': draft['hovered_champion'][draft['current_team']], 'role': draft['current_team'].lower()})
    else:
        available_champions = draft['remaining_champions']
        if available_champions:
            champion = random.choice(available_champions)
            draft['hovered_champion'][draft['current_team']] = champion
            lock_in_champion({'room': room, 'champion': champion, 'role': draft['current_team'].lower()})
        else:
            next_turn(room)

def all_picks_complete(draft):
    return len(draft['blue_team']['picks']) == 5 and len(draft['red_team']['picks']) == 5

def start_next_game(room):
    draft = drafts[room]
    draft['previous_drafts'].append({
        'blue_team': draft['blue_team'].copy(),
        'red_team': draft['red_team'].copy()
    })
    draft['current_game'] += 1
    draft['draft_order'] = get_draft_order(draft['current_game'])
    draft['draft_index'] = -1
    draft['blue_team']['picks'] = []
    draft['red_team']['picks'] = []
    draft['blue_team']['bans'] = []
    draft['red_team']['bans'] = []
    draft['blue_team']['ready'] = False
    draft['red_team']['ready'] = False
    draft['current_phase'] = 'Ready'
    draft['remaining_champions'] = champion_names.copy()
    draft['used_champions'] = set()
    draft['hovered_champion'] = {'Blue': None, 'Red': None}
    draft['side_swap_requested'] = {'Blue': False, 'Red': False}
    emit('update_draft', serialize_draft(draft), room=room, broadcast=True)

def end_draft(room):
    draft = drafts[room]
    draft['current_phase'] = 'Complete'
    emit('draft_complete', room=room)
    emit('update_draft', serialize_draft(draft), room=room, broadcast=True)

@socketio.on('request_side_swap')
def request_side_swap(data):
    room = data['room']
    team_name = data['team']
    draft = drafts[room]
    
    if draft['current_phase'] == 'Ready':
        if draft['blue_team']['name'] == team_name:
            draft['side_swap_requested']['Blue'] = True
        elif draft['red_team']['name'] == team_name:
            draft['side_swap_requested']['Red'] = True
        else:
            logging.error(f"Invalid team name received for side swap: {team_name}")
            return
        
        if all(draft['side_swap_requested'].values()):
            # Both teams have requested a side swap
            draft['blue_team'], draft['red_team'] = draft['red_team'], draft['blue_team']
            draft['side_swap_requested'] = {'Blue': False, 'Red': False}
            logging.info(f"Side swap performed in room {room}")
        emit('update_draft', serialize_draft(draft), room=room, broadcast=True)

#@app.before_request
#def log_request_info():
    #if request.method == 'GET':
       #logger.info('GET Request: %s', request.url)

if __name__ == '__main__':
    socketio.run(app, debug=True)