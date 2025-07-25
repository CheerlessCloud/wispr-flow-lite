#!/usr/bin/env python3
"""
Voice-to-Text Transcription App
A system-wide voice dictation tool that transcribes speech directly to cursor position.
Similar to WisprFlow functionality using OpenAI Whisper.

Features:
- Global hotkey (Option/Alt) to start/stop recording
- Transcribes speech using OpenAI Whisper API
- Types text directly at cursor position
- Removes filler words (um, uh, er, etc.)
- Cross-platform support (Windows, macOS, Linux)

Requirements:
pip install openai pyaudio keyboard pyautogui pynput python-dotenv
"""

import os
import sys
import time
import threading
import pyaudio
from openai import OpenAI
import pyautogui
import re
import tempfile
import wave
from pathlib import Path
from dotenv import load_dotenv
from pynput import keyboard
import logging
from tenacity import retry, stop_after_attempt, wait_exponential
import threading
from typing import Set, List, Optional, Union, Callable

# Configure logging
logging.basicConfig(level=logging.INFO,
                   format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

MAX_RETRIES = 3
RETRY_WAIT_SECONDS = 2

class HotkeyParser:
    """Parser for hotkey strings into pynput Key objects"""
    
    # Mapping of string names to pynput Key objects
    MODIFIER_MAP = {
        'ctrl': keyboard.Key.ctrl,
        'control': keyboard.Key.ctrl,
        'alt': keyboard.Key.alt,
        'option': keyboard.Key.alt,  # macOS naming
        'shift': keyboard.Key.shift,
        'cmd': keyboard.Key.cmd,     # macOS
        'win': keyboard.Key.cmd,     # Windows (same as cmd in pynput)
        'super': keyboard.Key.cmd,   # Linux
        'meta': keyboard.Key.cmd,    # Alternative naming
    }
    
    SPECIAL_KEYS_MAP = {
        'space': keyboard.Key.space,
        'tab': keyboard.Key.tab,
        'enter': keyboard.Key.enter,
        'return': keyboard.Key.enter,
        'backspace': keyboard.Key.backspace,
        'delete': keyboard.Key.delete,
        'esc': keyboard.Key.esc,
        'escape': keyboard.Key.esc,
    }
    
    @classmethod
    def parse_hotkey(cls, hotkey_string: str) -> Optional[Set[Union[keyboard.Key, keyboard.KeyCode]]]:
        """Parse a hotkey string into a set of pynput Key objects"""
        if not hotkey_string or not isinstance(hotkey_string, str):
            return None
            
        # Normalize the string: lowercase and split by +
        parts = [part.strip().lower() for part in hotkey_string.split('+')]
        
        if not parts:
            return None
            
        keys = set()
        
        for part in parts:
            key = cls._parse_single_key(part)
            if key is None:
                return None
            keys.add(key)
            
        return keys if keys else None
    
    @classmethod
    def _parse_single_key(cls, key_string: str) -> Optional[Union[keyboard.Key, keyboard.KeyCode]]:
        """Parse a single key string into a pynput Key object"""
        key_string = key_string.lower().strip()
        
        # Check modifiers first
        if key_string in cls.MODIFIER_MAP:
            return cls.MODIFIER_MAP[key_string]
            
        # Check special keys
        if key_string in cls.SPECIAL_KEYS_MAP:
            return cls.SPECIAL_KEYS_MAP[key_string]
            
        # Check function keys (f1-f24)
        import re
        if re.match(r'^f([1-9]|1[0-9]|2[0-4])$', key_string):
            fn_num = int(key_string[1:])
            # F1 is VK 112, F2 is 113, etc.
            return keyboard.KeyCode.from_vk(111 + fn_num)
            
        # Check single character keys
        if len(key_string) == 1 and key_string.isalnum():
            return keyboard.KeyCode.from_char(key_string)
            
        return None
    
    @classmethod
    def validate_hotkey(cls, hotkey_string: str) -> tuple[bool, str]:
        """Validate a hotkey string and return validation result"""
        if not hotkey_string:
            return False, "Hotkey string cannot be empty"
            
        keys = cls.parse_hotkey(hotkey_string)
        if keys is None:
            return False, f"Invalid hotkey format: '{hotkey_string}'"
            
        if len(keys) == 0:
            return False, "No valid keys found in hotkey string"
            
        if len(keys) > 4:
            return False, "Too many keys in combination (maximum 4)"
            
        return True, "Valid hotkey"

class HotkeyManager:
    """Advanced hotkey manager supporting key combinations"""
    
    def __init__(self):
        self.listener = None
        self.is_listening = False
        
        # Track currently pressed keys
        self.pressed_keys: Set[Union[keyboard.Key, keyboard.KeyCode]] = set()
        
        # Registered hotkey combinations
        self.hotkeys = {}
        
        # Debouncing
        self.debounce_ms = 100
        
        # Thread safety
        self.lock = threading.Lock()
    
    def register_hotkey(self, hotkey_string: str, on_press: Callable = None, on_release: Callable = None) -> bool:
        """Register a hotkey combination"""
        keys = HotkeyParser.parse_hotkey(hotkey_string)
        if keys is None:
            return False
            
        key_set = frozenset(keys)
        
        with self.lock:
            self.hotkeys[key_set] = {
                'on_press': on_press,
                'on_release': on_release,
                'hotkey_string': hotkey_string,
                'is_active': False,
                'last_triggered': 0
            }
            
        return True
    
    def _normalize_key(self, key: Union[keyboard.Key, keyboard.KeyCode]) -> Union[keyboard.Key, keyboard.KeyCode]:
        """Normalize keys for consistent comparison"""
        # Handle left/right modifier keys - treat them as the same
        if key == keyboard.Key.ctrl_l or key == keyboard.Key.ctrl_r:
            return keyboard.Key.ctrl
        elif key == keyboard.Key.alt_l or key == keyboard.Key.alt_r:
            return keyboard.Key.alt
        elif key == keyboard.Key.shift_l or key == keyboard.Key.shift_r:
            return keyboard.Key.shift
        elif key == keyboard.Key.cmd_l or key == keyboard.Key.cmd_r:
            return keyboard.Key.cmd
        return key
    
    def _on_press(self, key: Union[keyboard.Key, keyboard.KeyCode]):
        """Handle key press events"""
        key = self._normalize_key(key)
        
        with self.lock:
            self.pressed_keys.add(key)
            self._check_hotkey_matches()
    
    def _on_release(self, key: Union[keyboard.Key, keyboard.KeyCode]):
        """Handle key release events"""
        key = self._normalize_key(key)
        
        with self.lock:
            self.pressed_keys.discard(key)
            self._check_hotkey_releases()
    
    def _check_hotkey_matches(self):
        """Check if any registered hotkeys match current pressed keys"""
        for key_combination, hotkey_info in self.hotkeys.items():
            if key_combination.issubset(self.pressed_keys):
                if not hotkey_info['is_active']:
                    hotkey_info['is_active'] = True
                    if hotkey_info['on_press']:
                        self._trigger_callback(hotkey_info['on_press'])
    
    def _check_hotkey_releases(self):
        """Check for hotkey releases"""
        for key_combination, hotkey_info in self.hotkeys.items():
            if hotkey_info['is_active']:
                if not key_combination.issubset(self.pressed_keys):
                    hotkey_info['is_active'] = False
                    if hotkey_info['on_release']:
                        self._trigger_callback(hotkey_info['on_release'])
    
    def _trigger_callback(self, callback: Callable):
        """Trigger callback in a separate thread"""
        def run_callback():
            try:
                callback()
            except Exception as e:
                logger.error(f"Error in hotkey callback: {e}")
        
        threading.Thread(target=run_callback, daemon=True).start()
    
    def start_listening(self):
        """Start the keyboard listener"""
        if self.is_listening:
            return
            
        self.listener = keyboard.Listener(
            on_press=self._on_press,
            on_release=self._on_release
        )
        self.listener.start()
        self.is_listening = True
    
    def stop_listening(self):
        """Stop the keyboard listener"""
        if self.listener:
            self.listener.stop()
            self.listener = None
        self.is_listening = False
        
        with self.lock:
            self.pressed_keys.clear()
            for hotkey_info in self.hotkeys.values():
                hotkey_info['is_active'] = False

def get_input_device():
    """Find the best available input device"""
    audio = None
    try:
        audio = pyaudio.PyAudio()
        default_input = audio.get_default_input_device_info()
        logger.info(f"🎤 Using input device: {default_input['name']}")
        return default_input['index']
    except Exception as e:
        logger.warning(f"⚠️ Warning: Could not get default input device: {e}")
        # Try to find any working input device
        if audio:
            for i in range(audio.get_device_count()):
                try:
                    device_info = audio.get_device_info_by_index(i)
                    if device_info['maxInputChannels'] > 0:
                        logger.info(f"🎤 Using alternative input device: {device_info['name']}")
                        return i
                except Exception as dev_e:
                    logger.debug(f"Failed to check device {i}: {dev_e}")
                    continue
        return None
    finally:
        if audio:
            try:
                audio.terminate()
            except Exception as e:
                logger.error(f"Failed to terminate PyAudio: {e}")

class VoiceTranscriber:
    def __init__(self):
        # Initialize OpenAI client
        api_key = os.getenv('OPENAI_API_KEY')
        if not api_key or api_key == 'your-api-key-here':
            logger.error("❌ OpenAI API key not found!")
            print("Please set your API key in the .env file:")
            print("OPENAI_API_KEY=your-actual-api-key-here")
            sys.exit(1)
            
        self.client = OpenAI(api_key=api_key)
        
        # Audio recording settings
        self.chunk = int(os.getenv('CHUNK_SIZE', 2048))
        self.format = pyaudio.paInt16
        self.channels = 1
        self.rate = int(os.getenv('SAMPLE_RATE', 16000))
        self.record_seconds = int(os.getenv('MAX_RECORDING_TIME', 30))
        
        # Recording state
        self.is_recording = False
        self.audio_frames = []
        self.audio = None
        self.stream = None
        self.temp_files = set()  # Track temporary files
        
        # Initialize audio with retry
        self._initialize_audio()
        
        # Hotkey configuration
        self.hotkey_manager = HotkeyManager()
        self.hotkey_string = os.getenv('HOTKEY', 'alt')
        
        # Language setting
        self.language = os.getenv('LANGUAGE', 'en')
        
        # Transcription model setting
        self.transcription_model = os.getenv('TRANSCRIPTION_MODEL', 'whisper-1')
        
        # Filler words to remove
        self.filler_words = {
            'um', 'uh', 'er', 'ah', 'like', 'you know', 'so', 'well',
            'hmm', 'okay', 'right', 'actually', 'basically', 'literally',
            'i mean', 'sort of', 'kind of', 'you see'
        }
        
        # Add custom filler words from config
        custom_fillers = os.getenv('CUSTOM_FILLER_WORDS', '')
        if custom_fillers:
            custom_list = [word.strip() for word in custom_fillers.split(',')]
            self.filler_words.update(custom_list)
        
        # Setup hotkey
        self._setup_hotkey()
        
        logger.info(f"🎙️ Voice Transcriber initialized")
        logger.info(f"📋 Hotkey: {self.hotkey_string}")

        if self.language and self.language.lower() != 'auto':
            logger.info(f"🌍 Language: {self.language} (specific)")
        else:
            logger.info(f"🌍 Language: automatic detection (supports multilingual)")

        logger.info(f"🤖 Transcription model: {self.transcription_model}")
        logger.info(f"⏱️ Max recording time: {self.record_seconds}s")

    def _setup_hotkey(self):
        """Setup configurable hotkey from environment"""
        # Validate hotkey string
        is_valid, error_msg = HotkeyParser.validate_hotkey(self.hotkey_string)
        
        if not is_valid:
            logger.warning(f"⚠️ Invalid hotkey '{self.hotkey_string}': {error_msg}")
            logger.info("🔄 Falling back to default 'alt' hotkey")
            self.hotkey_string = 'alt'
        
        # Register hotkey
        success = self.hotkey_manager.register_hotkey(
            hotkey_string=self.hotkey_string,
            on_press=self.start_recording,
            on_release=self.stop_recording
        )
        
        if not success:
            logger.error(f"❌ Failed to register hotkey: {self.hotkey_string}")
            # Try fallback to alt
            self.hotkey_string = 'alt'
            success = self.hotkey_manager.register_hotkey(
                hotkey_string=self.hotkey_string,
                on_press=self.start_recording,
                on_release=self.stop_recording
            )
            
            if success:
                logger.info("✅ Fallback to 'alt' hotkey successful")
            else:
                raise Exception("Failed to setup any working hotkey")

    def _initialize_audio(self):
        """Initialize PyAudio with retry logic"""
        retry_count = 0
        while retry_count < MAX_RETRIES:
            try:
                self.input_device_index = get_input_device()
                if self.input_device_index is None:
                    raise Exception("No working input device found")

                self.audio = pyaudio.PyAudio()
                
                # Test audio setup
                test_stream = self.audio.open(
                    format=self.format,
                    channels=self.channels,
                    rate=self.rate,
                    input=True,
                    input_device_index=self.input_device_index,
                    frames_per_buffer=self.chunk,
                    start=False
                )
                test_stream.close()
                return
            except Exception as e:
                retry_count += 1
                logger.warning(f"Failed to initialize audio (attempt {retry_count}/{MAX_RETRIES}): {e}")
                if self.audio:
                    self.audio.terminate()
                if retry_count < MAX_RETRIES:
                    time.sleep(RETRY_WAIT_SECONDS)
                else:
                    logger.error("❌ Failed to initialize audio after multiple attempts")
                    raise

    def clean_text(self, text):
        """Remove filler words and clean up the transcribed text"""
        if not text:
            return ""
        
        logger.info(f"🧹 Cleaning text: '{text}'")
        
        # Split into words, preserving original case for final output
        original_words = text.split()
        
        # Remove filler words if enabled
        remove_fillers = os.getenv('REMOVE_FILLER_WORDS', 'true').lower() == 'true'
        if remove_fillers:
            cleaned_words = []
            i = 0
            while i < len(original_words):
                # Check for filler words using lowercase comparison but keep original case
                word_lower = original_words[i].strip('.,!?;:"()[]{}').lower()
                
                # Check for multi-word fillers like "you know", "i mean"
                skip = False
                for filler_length in [3, 2, 1]:  # Check longer phrases first
                    if i + filler_length <= len(original_words):
                        # Create phrase in lowercase for comparison
                        phrase_words = original_words[i:i+filler_length]
                        phrase_lower = ' '.join([w.strip('.,!?;:"()[]{}').lower() for w in phrase_words])
                        
                        # Only check English filler words to avoid removing Russian words
                        if phrase_lower in self.filler_words:
                            logger.debug(f"🗑️ Removing filler: '{' '.join(phrase_words)}'")
                            i += filler_length
                            skip = True
                            break
                
                if not skip:
                    cleaned_words.append(original_words[i])
                    i += 1
            
            original_words = cleaned_words
        
        # Reconstruct text
        cleaned_text = ' '.join(original_words)
        
        # Basic grammar improvements
        cleaned_text = self.improve_grammar(cleaned_text)
        
        logger.info(f"✨ Cleaned text: '{cleaned_text}'")
        return cleaned_text
    
    def improve_grammar(self, text):
        """Basic grammar improvements for multilingual text"""
        if not text:
            return ""
            
        # Capitalize first letter (works for both Latin and Cyrillic)
        text = text[0].upper() + text[1:] if len(text) > 1 else text.upper()
        
        # Capitalize after periods (works for any Unicode letters)
        text = re.sub(r'(\. )([a-zа-я])', lambda m: m.group(1) + m.group(2).upper(), text, flags=re.UNICODE)
        
        # Fix common spacing issues
        text = re.sub(r'\s+', ' ', text)  # Multiple spaces to single space
        text = re.sub(r'\s+([.!?,:;])', r'\1', text)  # Remove space before punctuation
        
        # Capitalize 'I' (English only to avoid affecting Russian)
        text = re.sub(r'\bi\b', 'I', text)
        
        return text.strip()
    
    def start_recording(self):
        """Start recording audio"""
        # Start recording in a separate thread
        recording_thread = threading.Thread(target=self._record_audio)
        recording_thread.daemon = True
        recording_thread.start()

    def _record_audio(self):
        """Internal method to handle the actual recording"""
        try:
            # Clean up any existing stream first
            self._cleanup_stream()

            self.stream = self.audio.open(
                format=self.format,
                channels=self.channels,
                rate=self.rate,
                input=True,
                input_device_index=self.input_device_index,
                frames_per_buffer=self.chunk
            )
            
            self.is_recording = True
            self.audio_frames = []
            
            logger.info(f"🎤 Recording... Release '{self.hotkey_string}' when done.")
            
            start_time = time.time()
            last_device_check = time.time()
            estimated_memory = 0
            max_memory_mb = float(os.getenv('MAX_MEMORY_MB', 100))  # Default 100MB limit
            
            # Record in chunks
            while self.is_recording:
                try:
                    # Periodic device check (every 2 seconds)
                    current_time = time.time()
                    if current_time - last_device_check > 2:
                        if not self._check_device_available():
                            logger.error("❌ Audio device became unavailable")
                            break
                        last_device_check = current_time

                    # Check if stream is still active
                    if not self.stream.is_active():
                        logger.error("❌ Audio stream became inactive")
                        break

                    # Read audio data
                    data = self.stream.read(self.chunk, exception_on_overflow=False)
                    self.audio_frames.append(data)
                    
                    # Estimate memory usage (2 bytes per sample)
                    chunk_memory = len(data)
                    estimated_memory += chunk_memory
                    if estimated_memory > max_memory_mb * 1024 * 1024:
                        logger.warning(f"⚠️ Memory limit reached ({max_memory_mb}MB)")
                        break
                    
                    # Check for maximum recording time
                    if time.time() - start_time > self.record_seconds:
                        logger.info(f"⏰ Maximum recording time ({self.record_seconds}s) reached")
                        break
                        
                except Exception as e:
                    logger.error(f"Error reading audio: {e}")
                    # Try to recover from transient errors
                    if "Input overflowed" in str(e):
                        logger.warning("⚠️ Input overflow detected, continuing recording")
                        continue
                    break
                
        except Exception as e:
            logger.error(f"❌ Error during recording: {e}")
            if "Invalid sample rate" in str(e):
                logger.info("💡 Try adjusting the sample rate in your .env file")
            elif "Device unavailable" in str(e):
                logger.info("💡 Check your microphone connection and permissions")
        finally:
            self._cleanup_stream()

    def _check_device_available(self):
        """Check if the audio device is still available"""
        try:
            audio = pyaudio.PyAudio()
            device_info = audio.get_device_info_by_index(self.input_device_index)
            audio.terminate()
            return device_info['maxInputChannels'] > 0
        except:
            return False

    def _cleanup_stream(self):
        """Clean up the audio stream"""
        if self.stream:
            try:
                self.stream.stop_stream()
            except Exception as e:
                logger.debug(f"Error stopping stream: {e}")
            try:
                self.stream.close()
            except Exception as e:
                logger.debug(f"Error closing stream: {e}")
            self.stream = None

    def stop_recording(self):
        """Stop recording audio"""
        self.is_recording = False
        time.sleep(0.1)  # Give a moment for the recording loop to finish
        self._cleanup_stream()

        if not self.audio_frames:
            logger.warning("⚠️ No audio data recorded")
            return

        # Process the recording in a separate thread
        processing_thread = threading.Thread(target=self._process_audio)
        processing_thread.daemon = True
        processing_thread.start()

    def _process_audio(self):
        """Process the recorded audio"""
        try:
            # Save audio to file
            audio_file = self.save_audio_to_file()
            if not audio_file:
                logger.error("❌ Failed to save audio file")
                return

            # Transcribe audio
            logger.info("🔄 Transcribing audio...")
            text = self.transcribe_audio(audio_file)
            
            if not text:
                logger.warning("⚠️ No text transcribed")
                return

            # Clean up the text
            cleaned_text = self.clean_text(text)
            
            # Type the text
            if cleaned_text:
                self.type_text(cleaned_text)
            else:
                logger.warning("⚠️ No text to type after cleaning")

        except Exception as e:
            logger.error(f"❌ Processing error: {e}")
        finally:
            # Cleanup is handled by the temp_files tracking system
            pass
    
    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
    def transcribe_audio(self, audio_file_path):
        """Transcribe audio file using OpenAI Whisper API with retry logic"""
        try:
            with open(audio_file_path, "rb") as audio_file:
                # Prepare transcription parameters
                transcription_params = {
                    "model": self.transcription_model,
                    "file": audio_file
                }
                
                # Add language parameter only if not using automatic detection
                if self.language and self.language.lower() != 'auto':
                    transcription_params["language"] = self.language
                    logger.debug(f"🌍 Using specified language: {self.language}")
                else:
                    logger.debug("🔍 Using automatic language detection")
                
                transcript = self.client.audio.transcriptions.create(**transcription_params)
                logger.info(f"📝 Raw transcription: '{transcript.text}'")
                return transcript.text
        except Exception as e:
            logger.error(f"Error during transcription: {e}")
            raise
    
    def save_audio_to_file(self):
        """Save recorded audio to a temporary file"""
        if not self.audio_frames:
            return None

        try:
            temp_file = tempfile.NamedTemporaryFile(delete=False, suffix='.wav')
            self.temp_files.add(temp_file.name)  # Track the temporary file

            with wave.open(temp_file.name, 'wb') as wf:
                wf.setnchannels(self.channels)
                wf.setsampwidth(self.audio.get_sample_size(self.format))
                wf.setframerate(self.rate)
                wf.writeframes(b''.join(self.audio_frames))

            return temp_file.name
        except Exception as e:
            logger.error(f"Error saving audio file: {e}")
            if temp_file and os.path.exists(temp_file.name):
                try:
                    os.remove(temp_file.name)
                except:
                    pass
            return None
    
    def type_text(self, text):
        """Type the transcribed text at the current cursor position"""
        try:
            # Small delay to ensure the cursor is ready
            time.sleep(0.1)
            
            # Get typing interval from config
            interval = float(os.getenv('TYPING_INTERVAL', 0.01))
            
            logger.info(f"⌨️ Typing text: '{text}'")
            
            # Determine the best typing method
            typing_method = os.getenv('TYPING_METHOD', 'auto').lower()
            
            if typing_method == 'auto':
                # Smart auto-detection: check if text contains non-ASCII characters
                has_non_ascii = not text.isascii()
                if has_non_ascii:
                    logger.info(f"🔍 Detected non-ASCII characters, using clipboard method")
                    typing_method = 'clipboard'
                else:
                    logger.info(f"🔍 Text is ASCII-only, using direct method")
                    typing_method = 'direct'
            
            if typing_method == 'clipboard':
                # Method 1: Use clipboard for Unicode support
                try:
                    import pyperclip
                    pyperclip.copy(text)
                    time.sleep(0.1)
                    # Paste using Ctrl+V
                    pyautogui.hotkey('ctrl', 'v')
                    logger.info("✅ Text typed using clipboard method")
                    return
                except Exception as clipboard_e:
                    logger.warning(f"⚠️ Clipboard method failed: {clipboard_e}")
                    # Fall back to direct method if clipboard fails
                    logger.info("🔄 Falling back to direct method...")
                    typing_method = 'direct'
            
            if typing_method == 'direct':
                # Method 2: Direct typing
                try:
                    pyautogui.write(text, interval=interval)
                    logger.info("✅ Text typed using direct method")
                    return
                except Exception as direct_e:
                    logger.warning(f"⚠️ Direct method failed: {direct_e}")
                    # Fall back to character-by-character method
                    logger.info("🔄 Falling back to character-by-character method...")
            
            # Method 3: Character-by-character with key codes (fallback)
            logger.info("🔄 Using character-by-character fallback...")
            for char in text:
                try:
                    if char.isascii():
                        pyautogui.write(char)
                    else:
                        # For non-ASCII characters, try typing via key events
                        pyautogui.typewrite([char])
                    time.sleep(interval)
                except Exception as char_e:
                    logger.warning(f"⚠️ Failed to type character '{char}': {char_e}")
                    # Skip problematic characters
                    continue
            
            logger.info("✅ Text typed using character-by-character method")
            
        except Exception as e:
            logger.error(f"❌ Error typing text: {e}")
            print(f"❌ Error typing text: {e}")
            print("💡 Make sure to click in a text field before recording")
            print("💡 Try setting TYPING_METHOD=clipboard in .env for better Unicode support")
    
    
    def run(self):
        """Main loop - listen for hotkey"""
        try:
            # Start hotkey manager
            self.hotkey_manager.start_listening()
            
            print(f"🎯 Ready! Press and hold '{self.hotkey_string}' to record, release to transcribe.")
            print("💡 Position your cursor where you want text to appear")
            print("⏹️ Press Ctrl+C to quit")
            print()
            
            # Keep the program running
            while True:
                time.sleep(0.1)
            
        except KeyboardInterrupt:
            print("\n👋 Goodbye!")
        except Exception as e:
            print(f"❌ Error: {e}")
            if "permissions" in str(e).lower():
                self.print_permission_help()
        finally:
            self.cleanup()
    
    def print_permission_help(self):
        """Print platform-specific permission help"""
        import platform
        system = platform.system()
        
        if system == "Darwin":  # macOS
            print("\n🍎 macOS Permission Required:")
            print("Go to System Preferences > Security & Privacy > Privacy")
            print("• Enable 'Microphone' access")
            print("• Enable 'Accessibility' access")
            print("• Enable 'Input Monitoring' access")
        elif system == "Linux":
            print("\n🐧 Linux Permission Required:")
            print("Add your user to the audio group:")
            print("sudo usermod -a -G audio $USER")
            print("Then logout and login again")
    
    def cleanup(self):
        """Clean up resources"""
        try:
            # Stop recording if still active
            if self.is_recording:
                self.stop_recording()

            # Clean up hotkey manager
            if self.hotkey_manager:
                self.hotkey_manager.stop_listening()

            # Clean up audio resources
            self._cleanup_stream()
            if self.audio:
                self.audio.terminate()

            # Clean up temporary files
            for temp_file in self.temp_files:
                try:
                    if os.path.exists(temp_file):
                        os.remove(temp_file)
                        logger.debug(f"Removed temporary file: {temp_file}")
                except Exception as e:
                    logger.warning(f"Failed to remove temporary file {temp_file}: {e}")

        except Exception as e:
            logger.error(f"Error during cleanup: {e}")

    def __del__(self):
        """Destructor to ensure cleanup"""
        self.cleanup()

def check_dependencies():
    """Check if all required packages are installed"""
    required = ['openai', 'pyaudio', 'pynput', 'pyautogui']
    missing = []
    
    for package in required:
        try:
            __import__(package)
        except ImportError:
            missing.append(package)
    
    if missing:
        print(f"❌ Missing packages: {', '.join(missing)}")
        print(f"Install with: pip install {' '.join(missing)} python-dotenv")
        return False
    return True

if __name__ == "__main__":
    print("🎙️ Voice-to-Text Transcription App")
    print("==================================")
    
    # Check dependencies
    if not check_dependencies():
        sys.exit(1)
    
    # Initialize and run
    try:
        transcriber = VoiceTranscriber()
        transcriber.run()
    except Exception as e:
        print(f"❌ Failed to start: {e}")
        sys.exit(1)
