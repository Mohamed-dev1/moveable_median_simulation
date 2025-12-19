"""
CARLA Traffic Simulation Dashboard Server
Real-time web-based control and monitoring system
"""

from flask import Flask, render_template, jsonify, request, session, redirect, url_for
from flask_socketio import SocketIO, emit
from flask_cors import CORS
import threading
import time
import json
import sys
import os
import random
import math
import io

# Fix Unicode encoding for Windows console
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

# Try to import CARLA (optional for testing)
try:
    sys.path.append('C:/Users/moham/Documents/CARLA/WindowsNoEditor/PythonAPI/carla/dist/carla-0.9.15-py3.7-win-amd64.egg')
    import carla
    CARLA_AVAILABLE = True
except:
    CARLA_AVAILABLE = False
    print("Warning: CARLA not available. Dashboard will run in demo mode.")

# Authentication credentials
USERS = {
    'admin': 'admin123',
    'student': 'student123',
    'observer': 'observer123'
}

app = Flask(__name__)
app.config['SECRET_KEY'] = 'carla-traffic-sim-secret-key-2025'
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*")

# Global simulation state
simulation_state = {
    'running': False,
    'mode': '3-3',  # or '4-2'
    'total_vehicles': 0,
    'forward_vehicles': 0,
    'backward_vehicles': 0,
    'forward_speed': 0.0,
    'backward_speed': 0.0,
    'congestion_level': 0.0,
    'time_elapsed': 0.0,
    'median_position': 0.0,
    'camera_view': 'Overview',
    'weather': 'Clear',
    'time_of_day': 'Noon',
    'lane_data': {
        'forward': [0, 0, 0, 0],
        'backward': [0, 0, 0, 0]
    },
    'speed_multiplier': 1.0,
    'spawn_rate_forward': 0.4,
    'spawn_rate_backward': 0.3,
    'auto_mode': True,
    'process_pid': None  # Store only PID (not Popen object - it's not JSON serializable)
}

# CARLA connection
carla_client = None
carla_world = None
carla_map = None
traffic_manager = None
median_actors = []
spawned_vehicles = []
spectator = None

# CARLA connection objects
carla_client = None
carla_world = None
carla_tm = None
vehicles = []
median = None
spectator = None
camera_positions = []
current_camera_index = 0
fpv_vehicle = None

class SimulationController:
    def __init__(self):
        self.running = False
        self.thread = None
        
    def connect_carla(self):
        global carla_client, carla_world, carla_tm, spectator
        try:
            carla_client = carla.Client('localhost', 2000)
            carla_client.set_timeout(10.0)
            carla_world = carla_client.get_world()
            carla_tm = carla_client.get_trafficmanager(8000)
            spectator = carla_world.get_spectator()
            
            # Setup synchronous mode
            settings = carla_world.get_settings()
            settings.synchronous_mode = True
            settings.fixed_delta_seconds = 0.05
            carla_world.apply_settings(settings)
            
            carla_tm.set_synchronous_mode(True)
            
            return True
        except Exception as e:
            print(f"Failed to connect to CARLA: {e}")
            return False
    
    def start_simulation(self):
        if not self.running:
            self.running = True
            self.thread = threading.Thread(target=self.simulation_loop)
            self.thread.daemon = True
            self.thread.start()
            
    def stop_simulation(self):
        self.running = False
        if self.thread:
            self.thread.join()
    
    def simulation_loop(self):
        """Main simulation loop running in background thread"""
        global simulation_state
        
        state_file = os.path.join(os.path.dirname(__file__), 'simulation_state.json')
        
        print("Dashboard monitoring loop started...")
        
        # Track if we've seen the state file
        file_seen = False
        last_modified = 0
        self.last_mode = None  # Track mode changes for automatic shift detection
        
        while self.running:
            try:
                # Read state from test_carla.py shared file
                if os.path.exists(state_file):
                    # Check if file was modified
                    current_modified = os.path.getmtime(state_file)
                    
                    if current_modified != last_modified:
                        try:
                            with open(state_file, 'r') as f:
                                file_state = json.load(f)
                                # Update simulation state with real data
                                simulation_state.update(file_state)
                                
                                if not file_seen:
                                    print(f"✓ Connected to simulation (vehicles: {file_state.get('total_vehicles', 0)})")
                                    file_seen = True
                                
                                last_modified = current_modified
                        except Exception as e:
                            pass
                else:
                    # File doesn't exist - simulation not running yet
                    if file_seen:
                        simulation_state['running'] = False
                        file_seen = False
                        print("⚠ Lost connection to simulation")
                
                # Emit updates to all connected clients
                # Make sure we don't try to serialize non-JSON objects
                safe_state = {k: v for k, v in simulation_state.items() if k != 'process_pid'}
                socketio.emit('simulation_update', safe_state)
                
                # If median position changed (automatic shift), notify all clients
                if hasattr(self, 'last_mode') and self.last_mode != simulation_state.get('mode'):
                    median_pos = simulation_state.get('median_position', 0)
                    socketio.emit('median_update', {
                        'position': median_pos,
                        'mode': simulation_state.get('mode', '3-3')
                    })
                    print(f"📡 Broadcasting automatic median shift: {simulation_state.get('mode')}")
                self.last_mode = simulation_state.get('mode')
                    
                time.sleep(0.5)  # Update every 500ms
                
            except Exception as e:
                # Don't spam errors
                if "JSON serializable" not in str(e):
                    print(f"Simulation loop error: {e}")
                time.sleep(1)
    
    def update_traffic_data(self):
        """Update traffic statistics from CARLA world"""
        global vehicles, simulation_state
        
        try:
            # Count vehicles and calculate speeds
            forward_speeds = []
            backward_speeds = []
            total_vehicles = len([v for v in vehicles if v.is_alive])
            
            for v in vehicles:
                if v.is_alive:
                    vel = v.get_velocity()
                    speed = 3.6 * math.sqrt(vel.x**2 + vel.y**2 + vel.z**2)
                    
                    # Classify by direction (simplified)
                    if abs(v.get_transform().rotation.yaw) < 90:
                        forward_speeds.append(speed)
                    else:
                        backward_speeds.append(speed)
            
            simulation_state['total_vehicles'] = total_vehicles
            simulation_state['forward_vehicles'] = len(forward_speeds)
            simulation_state['backward_vehicles'] = len(backward_speeds)
            simulation_state['forward_speed'] = sum(forward_speeds) / len(forward_speeds) if forward_speeds else 0
            simulation_state['backward_speed'] = sum(backward_speeds) / len(backward_speeds) if backward_speeds else 0
            
            # Calculate congestion
            slow_vehicles = sum(1 for s in forward_speeds if s < 10.0)
            simulation_state['congestion_level'] = (slow_vehicles / len(forward_speeds) * 100) if forward_speeds else 0
            
        except Exception as e:
            print(f"Traffic data update error: {e}")

controller = SimulationController()

# ============================================================================
# AUTHENTICATION ROUTES
# ============================================================================

@app.route('/')
def index():
    if 'username' in session:
        return render_template('dashboard.html', username=session['username'])
    return redirect(url_for('login'))

@app.route('/metrics')
def metrics_dashboard():
    if 'username' in session:
        return render_template('metrics_dashboard.html', username=session['username'])
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        if username in USERS and USERS[username] == password:
            session['username'] = username
            return redirect(url_for('index'))
        else:
            return render_template('login.html', error='Invalid credentials')
    
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.pop('username', None)
    return redirect(url_for('login'))

# ============================================================================
# API ROUTES
# ============================================================================

@app.route('/api/status')
def get_status():
    return jsonify(simulation_state)

@app.route('/api/start', methods=['POST'])
def start_simulation():
    if 'username' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    
    # Run test_carla.py script
    import subprocess
    import socket
    
    # Check if CARLA is running
    def is_carla_running():
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(2)
            result = sock.connect_ex(('localhost', 2000))
            sock.close()
            return result == 0
        except:
            return False
    
    if not is_carla_running():
        print("✗ CARLA is not running on localhost:2000")
        return jsonify({'error': 'CARLA is not running! Please start CarlaUE4.exe first.'}), 400
    
    script_path = os.path.join(os.path.dirname(__file__), 'test_carla.py')
    # Use venv Python (has pygame) - CARLA path is added in test_carla.py
    venv_python = r'C:/Users/moham/Documents/median_3d/venv/Scripts/python.exe'
    
    print(f"✓ CARLA detected on port 2000")
    print(f"Starting test_carla.py from: {script_path}")
    print(f"Using Python: {venv_python}")
    
    try:
        # Start test_carla.py in a completely separate process
        # Use CREATE_NEW_CONSOLE to run in a new window
        CREATE_NEW_CONSOLE = 0x00000010
        
        process = subprocess.Popen(
            [venv_python, script_path],
            cwd=os.path.dirname(__file__),
            creationflags=CREATE_NEW_CONSOLE
        )
        
        # Start the controller loop to read simulation data
        controller.start_simulation()
        
        # Store only the PID, not the process object (can't be JSON serialized)
        simulation_state['process_pid'] = process.pid
        simulation_state['running'] = True
        
        socketio.emit('simulation_started', {'success': True})
        
        print(f"Started test_carla.py (PID: {process.pid}) in new window")
        print(f"Started dashboard monitoring loop")
        return jsonify({'success': True, 'message': 'Simulation started in new window', 'pid': process.pid})
        
    except Exception as e:
        print(f"Failed to start test_carla.py: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': f'Failed to start simulation: {str(e)}'}), 500

@app.route('/api/stop', methods=['POST'])
def stop_simulation():
    if 'username' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    
    # Stop the test_carla.py process
    if 'process' in simulation_state and simulation_state['process']:
        try:
            simulation_state['process'].terminate()
            simulation_state['process'].wait(timeout=5)
            print("Stopped test_carla.py")
        except Exception as e:
            print(f"Error stopping process: {e}")
            simulation_state['process'].kill()
    
    controller.stop_simulation()
    simulation_state['running'] = False
    simulation_state['process'] = None
    
    socketio.emit('simulation_stopped', {'success': True})
    
    return jsonify({'success': True, 'message': 'Simulation stopped'})

@app.route('/api/median/shift', methods=['POST'])
def shift_median():
    if 'username' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    
    data = request.json
    direction = data.get('direction', 'left')
    amount = float(data.get('amount', 0))
    
    # Update median position
    simulation_state['median_position'] = amount
    
    # Determine mode based on position (use -2.0 threshold for 4-2 mode with 3m shift)
    if amount <= -2.0:
        simulation_state['mode'] = '4-2'  # Shifted left 3m: 4 forward, 2 backward
    elif amount >= 2.0:
        simulation_state['mode'] = '2-4'  # Shifted right 3m: 2 forward, 4 backward
    else:
        simulation_state['mode'] = '3-3'  # Center: 3 forward, 3 backward
    
    # Write command to file for test_carla.py to read
    try:
        command = {
            'action': 'shift_median',
            'amount': amount,
            'mode': simulation_state['mode'],
            'timestamp': time.time()
        }
        command_path = os.path.join(os.path.dirname(__file__), 'dashboard_commands.json')
        with open(command_path, 'w') as f:
            json.dump(command, f)
        print(f"✓ Sent median shift command: {simulation_state['mode']} ({amount}m)")
    except Exception as e:
        print(f"✗ Error writing command: {e}")
    
    # Broadcast update to ALL connected clients
    socketio.emit('median_update', {
        'position': amount,
        'mode': simulation_state['mode']
    })
    
    return jsonify({
        'success': True, 
        'position': amount,
        'mode': simulation_state['mode']
    })

@app.route('/api/spawn/forward', methods=['POST'])
def spawn_forward():
    if 'username' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    
    count = request.json.get('count', 10)
    
    # Write command for test_carla.py
    try:
        command = {
            'action': 'spawn_vehicles',
            'direction': 'forward',
            'count': count,
            'timestamp': time.time()
        }
        command_path = os.path.join(os.path.dirname(__file__), 'dashboard_commands.json')
        with open(command_path, 'w') as f:
            json.dump(command, f)
        print(f"✓ Sent spawn command: {count} forward vehicles")
    except Exception as e:
        print(f"✗ Error writing command: {e}")
    
    # Broadcast spawn event
    socketio.emit('vehicle_spawned', {
        'direction': 'forward',
        'count': count
    })
    
    return jsonify({'success': True, 'spawned': count})

@app.route('/api/spawn/backward', methods=['POST'])
def spawn_backward():
    if 'username' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    
    count = request.json.get('count', 10)
    
    # Write command for test_carla.py
    try:
        command = {
            'action': 'spawn_vehicles',
            'direction': 'backward',
            'count': count,
            'timestamp': time.time()
        }
        command_path = os.path.join(os.path.dirname(__file__), 'dashboard_commands.json')
        with open(command_path, 'w') as f:
            json.dump(command, f)
        print(f"✓ Sent spawn command: {count} backward vehicles")
    except Exception as e:
        print(f"✗ Error writing command: {e}")
    
    # Broadcast spawn event
    socketio.emit('vehicle_spawned', {
        'direction': 'backward',
        'count': count
    })
    
    return jsonify({'success': True, 'spawned': count})

@app.route('/api/speed/set', methods=['POST'])
def set_speed_multiplier():
    if 'username' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    
    multiplier = float(request.json.get('multiplier', 1.0))
    simulation_state['speed_multiplier'] = max(0.1, min(10.0, multiplier))
    
    # Write command for test_carla.py
    try:
        command = {
            'action': 'set_speed',
            'multiplier': simulation_state['speed_multiplier'],
            'timestamp': time.time()
        }
        command_path = os.path.join(os.path.dirname(__file__), 'dashboard_commands.json')
        with open(command_path, 'w') as f:
            json.dump(command, f)
        print(f"Speed multiplier set to {simulation_state['speed_multiplier']}x")
    except Exception as e:
        print(f"Error writing speed command: {e}")
    
    # Broadcast to all clients
    socketio.emit('speed_update', {
        'multiplier': simulation_state['speed_multiplier']
    })
    
    return jsonify({'success': True, 'multiplier': simulation_state['speed_multiplier']})

@app.route('/api/camera/switch', methods=['POST'])
def switch_camera():
    if 'username' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    
    view = request.json.get('view', 'overview')
    simulation_state['camera_view'] = view.capitalize()
    
    # Write command for test_carla.py
    try:
        command = {
            'action': 'camera_switch',
            'view': view,
            'timestamp': time.time()
        }
        command_path = os.path.join(os.path.dirname(__file__), 'dashboard_commands.json')
        with open(command_path, 'w') as f:
            json.dump(command, f)
        print(f"Camera view set to {view}")
    except Exception as e:
        print(f"Error writing camera command: {e}")
    
    # Broadcast to all clients
    socketio.emit('camera_update', {
        'view': simulation_state['camera_view']
    })
    
    return jsonify({'success': True, 'view': simulation_state['camera_view']})

@app.route('/api/weather/set', methods=['POST'])
def set_weather():
    if 'username' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    
    weather = request.json.get('weather', 'Clear')
    simulation_state['weather'] = weather
    
    # Write command for test_carla.py
    try:
        command = {
            'action': 'set_weather',
            'weather': weather,
            'timestamp': time.time()
        }
        command_path = os.path.join(os.path.dirname(__file__), 'dashboard_commands.json')
        with open(command_path, 'w') as f:
            json.dump(command, f)
        print(f"Weather set to {weather}")
    except Exception as e:
        print(f"Error writing weather command: {e}")
    
    # Broadcast to all clients
    socketio.emit('weather_update', {
        'weather': weather
    })
    
    return jsonify({'success': True, 'weather': weather})

@app.route('/api/congestion/create', methods=['POST'])
def create_congestion():
    if 'username' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    
    direction = request.json.get('direction', 'forward')
    intensity = request.json.get('intensity', 0.5)
    
    # Write command for test_carla.py
    try:
        command = {
            'action': 'create_congestion',
            'direction': direction,
            'intensity': intensity,
            'timestamp': time.time()
        }
        command_path = os.path.join(os.path.dirname(__file__), 'dashboard_commands.json')
        with open(command_path, 'w') as f:
            json.dump(command, f)
        print(f"Creating {intensity*100:.0f}% congestion in {direction} lanes")
    except Exception as e:
        print(f"Error writing congestion command: {e}")
    
    return jsonify({'success': True})

@app.route('/api/mode/toggle', methods=['POST'])
def toggle_mode():
    if simulation_state['mode'] == '3-3':
        simulation_state['mode'] = '4-2'
    else:
        simulation_state['mode'] = '3-3'
    
    return jsonify({'success': True, 'mode': simulation_state['mode']})

@app.route('/api/traffic-lights/toggle', methods=['POST'])
def toggle_traffic_lights():
    if 'username' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    
    state = request.json.get('state', 'green')
    
    # Write command for test_carla.py
    try:
        command = {
            'action': 'traffic_lights',
            'state': state,
            'timestamp': time.time()
        }
        command_path = os.path.join(os.path.dirname(__file__), 'dashboard_commands.json')
        with open(command_path, 'w') as f:
            json.dump(command, f)
        print(f"Traffic lights set to {state}")
    except Exception as e:
        print(f"Error writing traffic light command: {e}")
    
    return jsonify({'success': True})

# ============================================================================
# METRICS API ENDPOINTS
# ============================================================================

@app.route('/api/metrics/current')
def get_current_metrics():
    """Get current simulation metrics"""
    try:
        # Try to read from simulation_results.json
        if os.path.exists('simulation_results.json'):
            with open('simulation_results.json', 'r') as f:
                history = json.load(f)
                if history:
                    latest = history[-1]
                    return jsonify({
                        'success': True,
                        'metrics': latest.get('metrics', {}),
                        'timestamp': latest.get('timestamp'),
                        'session_id': latest.get('session_id')
                    })
        
        # No data available - return error
        return jsonify({
            'success': False,
            'error': 'No simulation data found. Please run a simulation first.'
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/metrics/history')
def get_metrics_history():
    """Get historical metrics data"""
    try:
        if os.path.exists('simulation_results.json'):
            with open('simulation_results.json', 'r') as f:
                history = json.load(f)
                return jsonify({
                    'success': True,
                    'history': history,
                    'count': len(history)
                })
        return jsonify({'success': True, 'history': [], 'count': 0})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/metrics/summary')
def get_metrics_summary():
    """Get summary statistics across all simulation runs"""
    try:
        if os.path.exists('simulation_results.json'):
            with open('simulation_results.json', 'r') as f:
                history = json.load(f)
                if not history:
                    return jsonify({'success': True, 'summary': {}})
                
                # Calculate averages
                trip_improvements = [h['metrics']['trip_time_improvement_percent'] for h in history if 'metrics' in h]
                fuel_improvements = [h['metrics']['fuel_improvement_percent'] for h in history if 'metrics' in h]
                
                summary = {
                    'total_simulations': len(history),
                    'avg_trip_time_improvement': round(sum(trip_improvements) / len(trip_improvements), 2) if trip_improvements else 0,
                    'avg_fuel_improvement': round(sum(fuel_improvements) / len(fuel_improvements), 2) if fuel_improvements else 0,
                    'latest_session': history[-1].get('session_id'),
                    'latest_timestamp': history[-1].get('timestamp')
                }
                
                return jsonify({'success': True, 'summary': summary})
        return jsonify({'success': True, 'summary': {}})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

# ============================================================================
# SOCKETIO EVENTS
# ============================================================================

@socketio.on('connect')
def handle_connect():
    print('Client connected')
    emit('simulation_update', simulation_state)

@socketio.on('disconnect')
def handle_disconnect():
    print('Client disconnected')

if __name__ == '__main__':
    print("="*60)
    print("  CARLA Traffic Simulation Dashboard")
    print("  Web-based Real-time Control System")
    print("="*60)
    print("\n Starting server on http://localhost:5000")
    print(" Login credentials:")
    print("   - admin / admin123")
    print("   - student / student123")
    print("   - observer / observer123")
    print("\n Make sure CARLA is running on localhost:2000")
    print("="*60 + "\n")
    
    socketio.run(app, host='0.0.0.0', port=5000, debug=True)
