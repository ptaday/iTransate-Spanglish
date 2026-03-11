package com.assemblyai;

import com.google.gson.Gson;
import com.google.gson.JsonObject;
import org.java_websocket.client.WebSocketClient;
import org.java_websocket.handshake.ServerHandshake;

import javax.sound.sampled.*;
import java.io.*;
import java.net.URI;
import java.nio.ByteBuffer;
import java.time.Instant;
import java.time.ZoneId;
import java.time.format.DateTimeFormatter;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.atomic.AtomicBoolean;

public class Spanglish {
    // Configuration
    private static final String API_KEY = "1b5271944fcd40ffabdb562428925d48";
    private static final int SAMPLE_RATE = 16000;
    private static final int CHANNELS = 1;
    private static final int SAMPLE_SIZE_IN_BITS = 16;
    // FIX 4: increased from 400 (25ms) to 800 (50ms) — AssemblyAI requires minimum 50ms audio chunks
    private static final int FRAMES_PER_BUFFER = 800; // 50ms of audio (0.05s * 16000Hz)

    private static final String API_ENDPOINT = String.format(
        // FIX 2: changed encoding=opus to encoding=pcm_s16le — code sends raw PCM from mic, not Opus-encoded audio
        // FIX 3: added required speech_model parameter — v3 API rejects connections without it (status 3006)
        "wss://streaming.assemblyai.com/v3/ws?speech_model=u3-rt-pro&sample_rate=%d&format_turns=true&encoding=pcm_s16le",
        SAMPLE_RATE
    );

    // Audio recording
    private TargetDataLine microphone;
    private final List<byte[]> recordedFrames = new ArrayList<>();
    private final AtomicBoolean isRecording = new AtomicBoolean(false);
    private final AtomicBoolean stopRequested = new AtomicBoolean(false);
    private final Gson gson = new Gson();
    private AssemblyAIWebSocketClient wsClient;
    private Thread audioThread;

    public static void main(String[] args) {
        // Fix 1: instantiate the correct class (was StreamingTranscription, class is named Spanglish)
        Spanglish transcription = new Spanglish();
        transcription.run();
    }

    public void run() {
        System.out.println("Starting AssemblyAI real-time transcription...");
        System.out.println("Audio will be saved to a WAV file when the session ends.");

        try {
            // Initialize microphone
            initializeMicrophone();

            // Connect to WebSocket
            connectWebSocket();

            // Wait for user to press Ctrl+C
            CountDownLatch latch = new CountDownLatch(1);
            Runtime.getRuntime().addShutdownHook(new Thread(() -> {
                System.out.println("\nCtrl+C received. Stopping...");
                stopRequested.set(true);
                cleanup();
                latch.countDown();
            }));

            System.out.println("Speak into your microphone. Press Ctrl+C to stop.");
            latch.await();
        } catch (Exception e) {
            System.err.println("Error: " + e.getMessage());
            e.printStackTrace();
            cleanup();
        }
    }

    private void initializeMicrophone() throws LineUnavailableException {
        AudioFormat format = new AudioFormat(
                SAMPLE_RATE,
                SAMPLE_SIZE_IN_BITS,
                CHANNELS,
                true, // signed
                false // little endian
        );

        DataLine.Info info = new DataLine.Info(TargetDataLine.class, format);

        if (!AudioSystem.isLineSupported(info)) {
            throw new LineUnavailableException("Microphone not supported");
        }

        microphone = (TargetDataLine) AudioSystem.getLine(info);
        microphone.open(format, FRAMES_PER_BUFFER * 2);
        System.out.println("Microphone initialized successfully.");
    }

    private void connectWebSocket() throws Exception {
        URI uri = new URI(API_ENDPOINT);
        Map<String, String> headers = new HashMap<>();
        headers.put("Authorization", API_KEY);

        wsClient = new AssemblyAIWebSocketClient(uri, headers);
        wsClient.connectBlocking();
    }

    private void startAudioStreaming() {
        isRecording.set(true);
        microphone.start();

        audioThread = new Thread(() -> {
            System.out.println("Starting audio streaming...");
            byte[] buffer = new byte[FRAMES_PER_BUFFER * 2]; // 2 bytes per sample (16-bit)

            while (!stopRequested.get() && isRecording.get()) {
                try {
                    int bytesRead = microphone.read(buffer, 0, buffer.length);

                    if (bytesRead > 0) {
                        // Store for WAV file
                        byte[] audioData = new byte[bytesRead];
                        System.arraycopy(buffer, 0, audioData, 0, bytesRead);

                        synchronized (recordedFrames) {
                            recordedFrames.add(audioData);
                        }

                        // Send to WebSocket
                        if (wsClient != null && wsClient.isOpen()) {
                            wsClient.send(audioData);
                        }
                    }
                } catch (Exception e) {
                    if (!stopRequested.get()) {
                        System.err.println("Error streaming audio: " + e.getMessage());
                    }
                    break;
                }
            }

            System.out.println("Audio streaming stopped.");
        });

        audioThread.start();
    }

    private void cleanup() {
        stopRequested.set(true);
        isRecording.set(false);

        // Stop microphone
        if (microphone != null) {
            if (microphone.isActive()) {
                microphone.stop();
            }
            microphone.close();
        }

        // Wait for audio thread to finish
        if (audioThread != null && audioThread.isAlive()) {
            try {
                audioThread.join(1000);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            }
        }

        // Close WebSocket
        if (wsClient != null && wsClient.isOpen()) {
            try {
                // Send termination message
                JsonObject terminateMsg = new JsonObject();
                terminateMsg.addProperty("type", "Terminate");
                wsClient.send(gson.toJson(terminateMsg));
                Thread.sleep(500); // Give time for message to send
                wsClient.closeBlocking();
            } catch (Exception e) {
                System.err.println("Error closing WebSocket: " + e.getMessage());
            }
        }

        // Save WAV file
        saveWavFile();
        System.out.println("Cleanup complete. Exiting.");
    }

    private void saveWavFile() {
        if (recordedFrames.isEmpty()) {
            System.out.println("No audio data recorded.");
            return;
        }

        String timestamp = DateTimeFormatter.ofPattern("yyyyMMdd_HHmmss")
                .withZone(ZoneId.systemDefault())
                .format(Instant.now());
        String filename = "recorded_audio_" + timestamp + ".wav";

        try {
            // Calculate total data size
            int totalDataSize = 0;
            synchronized (recordedFrames) {
                for (byte[] frame : recordedFrames) {
                    totalDataSize += frame.length;
                }
            }

            // Create audio format
            AudioFormat format = new AudioFormat(
                    SAMPLE_RATE,
                    SAMPLE_SIZE_IN_BITS,
                    CHANNELS,
                    true,
                    false
            );

            // Write to file
            File wavFile = new File(filename);
            try (FileOutputStream fos = new FileOutputStream(wavFile);
                 BufferedOutputStream bos = new BufferedOutputStream(fos)) {

                // Write WAV header
                writeWavHeader(bos, totalDataSize);

                // Write audio data
                synchronized (recordedFrames) {
                    for (byte[] frame : recordedFrames) {
                        bos.write(frame);
                    }
                }
            }

            double durationSeconds = (double) totalDataSize / (SAMPLE_RATE * CHANNELS * 2);
            System.out.printf("Audio saved to: %s%n", filename);
            System.out.printf("Duration: %.2f seconds%n", durationSeconds);
        } catch (IOException e) {
            System.err.println("Error saving WAV file: " + e.getMessage());
        }
    }

    private void writeWavHeader(OutputStream out, int dataSize) throws IOException {
        ByteBuffer buffer = ByteBuffer.allocate(44);
        buffer.order(java.nio.ByteOrder.LITTLE_ENDIAN);

        // RIFF header
        buffer.put("RIFF".getBytes());
        buffer.putInt(36 + dataSize);
        buffer.put("WAVE".getBytes());

        // fmt chunk
        buffer.put("fmt ".getBytes());
        buffer.putInt(16); // fmt chunk size
        buffer.putShort((short) 1); // PCM format
        buffer.putShort((short) CHANNELS);
        buffer.putInt(SAMPLE_RATE);
        buffer.putInt(SAMPLE_RATE * CHANNELS * 2); // byte rate
        buffer.putShort((short) (CHANNELS * 2)); // block align
        buffer.putShort((short) SAMPLE_SIZE_IN_BITS);

        // data chunk
        buffer.put("data".getBytes());
        buffer.putInt(dataSize);

        out.write(buffer.array());
    }

    // Inner class for WebSocket client
    private class AssemblyAIWebSocketClient extends WebSocketClient {

        public AssemblyAIWebSocketClient(URI serverUri, Map<String, String> headers) {
            super(serverUri, headers);
        }

        @Override
        public void onOpen(ServerHandshake handshake) {
            System.out.println("WebSocket connection opened.");
            System.out.println("Connected to: " + API_ENDPOINT);
            startAudioStreaming();
        }

        @Override
        public void onMessage(String message) {
            try {
                JsonObject data = gson.fromJson(message, JsonObject.class);
                String msgType = data.get("type").getAsString();

                switch (msgType) {
                    case "Begin":
                        handleBeginMessage(data);
                        break;
                    case "Turn":
                        handleTurnMessage(data);
                        break;
                    case "Termination":
                        handleTerminationMessage(data);
                        break;
                    default:
                        // Ignore unknown message types
                        break;
                }
            } catch (Exception e) {
                System.err.println("Error handling message: " + e.getMessage());
            }
        }

        private void handleBeginMessage(JsonObject data) {
            String sessionId = data.get("id").getAsString();
            long expiresAt = data.get("expires_at").getAsLong();

            Instant instant = Instant.ofEpochSecond(expiresAt);
            String formattedTime = DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm:ss")
                    .withZone(ZoneId.systemDefault())
                    .format(instant);

            System.out.printf("%nSession began: ID=%s, ExpiresAt=%s%n", sessionId, formattedTime);
        }

        private void handleTurnMessage(JsonObject data) {
            String transcript = data.has("transcript") ? data.get("transcript").getAsString() : "";
            boolean formatted = data.has("turn_is_formatted") && data.get("turn_is_formatted").getAsBoolean();

            if (formatted) {
                // Clear line and print formatted transcript
                System.out.print("\r" + " ".repeat(80) + "\r");
                System.out.println(transcript);
            } else {
                // Print partial transcript on same line
                System.out.print("\r" + transcript);
            }
        }

        private void handleTerminationMessage(JsonObject data) {
            double audioDuration = data.has("audio_duration_seconds")
                    ? data.get("audio_duration_seconds").getAsDouble() : 0.0;
            double sessionDuration = data.has("session_duration_seconds")
                    ? data.get("session_duration_seconds").getAsDouble() : 0.0;

            System.out.printf("%nSession Terminated: Audio Duration=%.2fs, Session Duration=%.2fs%n",
                    audioDuration, sessionDuration);
        }

        @Override
        public void onClose(int code, String reason, boolean remote) {
            System.out.printf("%nWebSocket Disconnected: Status=%d, Msg=%s%n", code, reason);
            stopRequested.set(true);
        }

        @Override
        public void onError(Exception ex) {
            System.err.println("\nWebSocket Error: " + ex.getMessage());
            stopRequested.set(true);
        }
    }
}
