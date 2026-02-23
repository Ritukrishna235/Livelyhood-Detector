import cv2
import torch
import RPi.GPIO as GPIO
import time
from RPLCD.i2c import CharLCD
import threading
import warnings

# Suppress torch FutureWarnings
warnings.filterwarnings("ignore", category=FutureWarning, module="torch")

# -----------------------------
# GPIO Setup with Better Error Handling
# -----------------------------
GPIO.setwarnings(False)
GPIO.cleanup()
time.sleep(0.5)  # Give GPIO time to clean up
GPIO.setmode(GPIO.BCM)

# Motor pins
AIN1 = 23
AIN2 = 24
PWMA = 18
STBY = 25

# Encoder pins
ENCODER_A = 17
ENCODER_B = 27

# Setup motor pins
GPIO.setup(AIN1, GPIO.OUT)
GPIO.setup(AIN2, GPIO.OUT)
GPIO.setup(PWMA, GPIO.OUT)
GPIO.setup(STBY, GPIO.OUT)

# Setup encoder pins with retry
encoder_setup_success = False
for attempt in range(3):
    try:
        GPIO.setup(ENCODER_A, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        GPIO.setup(ENCODER_B, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        encoder_setup_success = True
        print(f"Encoder pins setup successful on attempt {attempt + 1}")
        break
    except Exception as e:
        print(f"Encoder setup attempt {attempt + 1} failed: {e}")
        GPIO.cleanup()
        time.sleep(1)
        GPIO.setmode(GPIO.BCM)
        # Re-setup motor pins
        GPIO.setup(AIN1, GPIO.OUT)
        GPIO.setup(AIN2, GPIO.OUT)
        GPIO.setup(PWMA, GPIO.OUT)
        GPIO.setup(STBY, GPIO.OUT)

if not encoder_setup_success:
    print("WARNING: Could not setup encoder pins. RPM will not work.")

# Enable motor driver
GPIO.output(STBY, GPIO.HIGH)

# PWM for motor
pwm = GPIO.PWM(PWMA, 1000)
pwm.start(0)

# -----------------------------
# Enhanced Encoder Setup
# -----------------------------
pulse_count = 0
total_pulses = 0
measurement_pulses = 0  # Separate counter for RPM measurement
encoder_active = False
last_rpm_time = time.time()
rpm_lock = threading.Lock()
current_rpm_global = 0

def encoder_callback(channel):
    global pulse_count, total_pulses, measurement_pulses
    with rpm_lock:
        pulse_count += 1
        total_pulses += 1
        measurement_pulses += 1

# Try to add interrupt with multiple approaches
if encoder_setup_success:
    interrupt_success = False

    # Method 1: Try with rising edge
    try:
        GPIO.add_event_detect(ENCODER_A, GPIO.RISING, callback=encoder_callback, bouncetime=5)
        print("Encoder interrupt setup successful (RISING)")
        encoder_active = True
        interrupt_success = True
    except Exception as e:
        print(f"RISING edge setup failed: {e}")

        # Method 2: Try with both edges
        try:
            GPIO.add_event_detect(ENCODER_A, GPIO.BOTH, callback=encoder_callback, bouncetime=2)
            print("Encoder interrupt setup successful (BOTH)")
            encoder_active = True
            interrupt_success = True
        except Exception as e:
            print(f"BOTH edges setup failed: {e}")

    if not interrupt_success:
        print("WARNING: Interrupt-based encoder failed. Will use polling method.")
        encoder_active = False

# -----------------------------
# Polling-based encoder reading (backup method)
# -----------------------------
last_encoder_state = 0

def poll_encoder():
    global pulse_count, total_pulses, measurement_pulses, last_encoder_state

    if not encoder_setup_success:
        return

    try:
        current_state = GPIO.input(ENCODER_A)
        if current_state != last_encoder_state and current_state == 1:
            with rpm_lock:
                pulse_count += 1
                total_pulses += 1
                measurement_pulses += 1
        last_encoder_state = current_state
    except Exception as e:
        print(f"Encoder polling error: {e}")

# -----------------------------
# LCD Setup
# -----------------------------
try:
    lcd = CharLCD('PCF8574', 0x27)
    print("LCD initialized successfully")
except Exception as e:
    print(f"LCD initialization failed: {e}")
    lcd = None

def display_message(line1, line2=""):
    if lcd:
        try:
            lcd.clear()
            # Ensure lines fit LCD width (typically 16 characters)
            line1_display = line1[:16] if len(line1) > 16 else line1
            line2_display = line2[:16] if len(line2) > 16 else line2

            lcd.write_string(line1_display)
            if line2_display:
                lcd.crlf()
                lcd.write_string(line2_display)
        except Exception as e:
            print(f"LCD display error: {e}")

    # Always print to console for debugging
    print(f"LCD: {line1} | {line2}")

# -----------------------------
# RPM Calculation Thread
# -----------------------------
def rpm_calculation_thread():
    global current_rpm_global, measurement_pulses
    PULSES_PER_REV = 20  # Based on test results showing ~3 RPM with 20 PPR looks most reasonable

    print("RPM calculation thread started - waiting for encoder pulses...")

    # RPM smoothing variables
    rpm_history = []
    SMOOTHING_WINDOW = 3  # Average over 3 seconds for smoother display

    while True:
        try:
            time.sleep(1.0)  # Calculate RPM every second

            with rpm_lock:
                # Get pulses counted in the last second
                pulses_in_last_second = measurement_pulses

                # Reset measurement counter for next second
                measurement_pulses = 0

                # Calculate instant RPM
                instant_rpm = (pulses_in_last_second * 60) / PULSES_PER_REV if PULSES_PER_REV > 0 else 0

                # Add to history for smoothing
                rpm_history.append(instant_rpm)
                if len(rpm_history) > SMOOTHING_WINDOW:
                    rpm_history.pop(0)

                # Calculate smoothed RPM (average of recent readings)
                current_rpm_global = sum(rpm_history) / len(rpm_history)

                # Calculate RPM with different encoder configurations for debugging
                rpm_20 = (pulses_in_last_second * 60) / 20
                rpm_100 = (pulses_in_last_second * 60) / 100
                rpm_200 = (pulses_in_last_second * 60) / 200
                rpm_400 = (pulses_in_last_second * 60) / 400

                # Reduced debugging frequency - only print every 3 seconds when motor is running
                if pulses_in_last_second > 0 or total_pulses == 0:
                    print(f"=== RPM CALCULATION DEBUG ===")
                    print(f"Raw pulses in last second: {pulses_in_last_second}")
                    print(f"Total pulses since start: {total_pulses}")
                    print(f"Instant RPM: {instant_rpm:.1f} | Smoothed RPM: {current_rpm_global:.1f}")
                    print(f"RPM options - 20PPR: {rpm_20:.1f} | 100PPR: {rpm_100:.1f} | 200PPR: {rpm_200:.1f} | 400PPR: {rpm_400:.1f}")
                    print("=============================")

                # If no pulses detected for extended time, print helpful message
                if pulses_in_last_second == 0 and total_pulses == 0:
                    print("WARNING: No encoder pulses detected. Check encoder wiring and connections.")

        except Exception as e:
            print(f"RPM calculation thread error: {e}")
            time.sleep(0.1)

# -----------------------------
# Motor Functions
# -----------------------------
def motor_forward(speed=50):
    GPIO.output(AIN1, GPIO.HIGH)
    GPIO.output(AIN2, GPIO.LOW)
    pwm.ChangeDutyCycle(speed)

def motor_stop():
    GPIO.output(AIN1, GPIO.LOW)
    GPIO.output(AIN2, GPIO.LOW)
    pwm.ChangeDutyCycle(0)

# -----------------------------
# Test motor and encoder
# -----------------------------
print("Testing encoder connection...")
if encoder_setup_success:
    print("Testing encoder with motor running for 3 seconds...")

    # Start motor for testing
    motor_forward(50)

    start_test = time.time()
    initial_pulse_count = pulse_count

    while time.time() - start_test < 3.0:
        if not encoder_active:
            poll_encoder()
        state_a = GPIO.input(ENCODER_A)
        state_b = GPIO.input(ENCODER_B)
        print(f"Test - Encoder A: {state_a}, Encoder B: {state_b}, Pulse count: {pulse_count}")
        time.sleep(0.1)

    # Stop motor after test
    motor_stop()

    pulses_detected = pulse_count - initial_pulse_count
    print(f"Encoder test complete: {pulses_detected} pulses detected in 3 seconds")

    if pulses_detected == 0:
        print("WARNING: No encoder movement detected. Check:")
        print("1. Encoder wiring (A->GPIO17, B->GPIO27, VCC->3.3V, GND->GND)")
        print("2. Motor and encoder mechanical coupling")
        print("3. Encoder power supply")
        print("Starting with simulated encoder pulses for testing...")

        # Add some simulated pulses for testing when no real encoder
        with rpm_lock:
            measurement_pulses = 50  # Simulate 50 pulses per second
            total_pulses = 150
        print("Added simulated encoder pulses for RPM display testing")
    else:
        print(f"Encoder working! Detected {pulses_detected/3:.1f} pulses per second")
        estimated_rpm_20 = (pulses_detected/3 * 60) / 20
        estimated_rpm_100 = (pulses_detected/3 * 60) / 100
        print(f"Estimated RPM: {estimated_rpm_20:.1f} (20 PPR) or {estimated_rpm_100:.1f} (100 PPR)")
        print(f"Using 20 PPR configuration for RPM calculation (most suitable for your encoder)")

# -----------------------------
# Load YOLOv5 Model (Person only)
# -----------------------------
model = torch.hub.load('ultralytics/yolov5', 'yolov5n', pretrained=True)
model.classes = [0]  # 0 = person

# -----------------------------
# Camera Setup
# -----------------------------
cap = cv2.VideoCapture(0)
if not cap.isOpened():
    print("Camera not detected!")
    exit()

# -----------------------------
# Main Loop
# -----------------------------
try:
    display_message("System Starting", "Please wait...")
    time.sleep(2)

    # Start RPM calculation thread
    rpm_thread = threading.Thread(target=rpm_calculation_thread, daemon=True)
    rpm_thread.start()
    print("RPM calculation thread started")

    base_speed = 70
    reduced_speed = 30
    last_rpm = -1
    last_status = ""

    while True:
        try:
            ret, frame = cap.read()
            if not ret:
                print("Warning: Failed to read from camera")
                continue

            # YOLO detection
            results = model(frame)
            detections = results.xyxy[0]
            person_detected = any(int(cls) == 0 for cls in detections[:, -1].tolist())

            # IMMEDIATE speed change when object detected
            if person_detected:
                motor_forward(reduced_speed)
                status = "HUMAN DETECTED"  # Shorter for LCD
            else:
                motor_forward(base_speed)
                status = "RUNNING NORMAL"  # Shorter for LCD

            # Get current RPM from thread (thread-safe)
            with rpm_lock:
                current_rpm = current_rpm_global

            # Update LCD when status changes or RPM changes significantly
            rpm_rounded = round(current_rpm, 1)  # Round to 1 decimal place
            if abs(rpm_rounded - last_rpm) >= 0.5 or status != last_status:
                # Format RPM display nicely
                if rpm_rounded >= 10:
                    rpm_display = f"RPM: {int(rpm_rounded)}"
                else:
                    rpm_display = f"RPM: {rpm_rounded:.1f}"

                display_message(rpm_display, status)
                last_rpm = rpm_rounded
                last_status = status

            # Poll encoder if not using interrupts (non-blocking)
            if not encoder_active and encoder_setup_success:
                poll_encoder()

            # Small delay to prevent excessive CPU usage - now much faster response
            time.sleep(0.05)  # 50ms delay for responsive object detection

        except Exception as e:
            print(f"Error in main loop: {e}")
            motor_stop()  # Stop motor on error for safety
            time.sleep(0.1)  # Brief pause before retrying

except KeyboardInterrupt:
    print("Stopped by user")

finally:
    motor_stop()
    pwm.stop()
    cap.release()
    GPIO.cleanup()
    if lcd:
        lcd.clear()
    print("Cleanup complete")