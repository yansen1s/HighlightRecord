#include "BluetoothSerial.h"

BluetoothSerial SerialBT;

const int buttonPin = 12;
const int RecordON = 13;
const int HighlightInd = 15;

int lastState = HIGH;
bool isRecording = false;
bool btConnected = false;

void setup() {
  pinMode(buttonPin, INPUT_PULLUP);
  pinMode(RecordON, OUTPUT);
  pinMode(HighlightInd, OUTPUT);

  Serial.begin(115200);

  // Indikator awal boot: nyalakan semua LED
  digitalWrite(RecordON, HIGH);
  digitalWrite(HighlightInd, HIGH);

  // ESP32 sebagai Bluetooth Serial dengan nama RemoteESP
  SerialBT.begin("RemoteESP");  
  Serial.println("ESP32 siap, pairing Bluetooth...");
}

void loop() {
  // Cek status koneksi Bluetooth
  if (SerialBT.hasClient()) {
    if (!btConnected) {
      btConnected = true;
      Serial.println("Bluetooth Connected!");
      // Matikan indikator setelah konek
      digitalWrite(RecordON, LOW);
      digitalWrite(HighlightInd, LOW);
    }
  } else {
    if (btConnected) {
      btConnected = false;
      Serial.println("Bluetooth Disconnected!");
      // Nyalakan indikator lagi saat putus
      digitalWrite(RecordON, HIGH);
      digitalWrite(HighlightInd, HIGH);
    }
  }

  // Tombol ditekan → kirim highlight ke Raspi
  int buttonState = digitalRead(buttonPin);
  if (lastState == HIGH && buttonState == LOW) {
    Serial.println("Tombol ditekan → kirim highlight");
    SerialBT.println("highlight");
  }
  lastState = buttonState;

  // Baca perintah dari Raspi
  if (SerialBT.available()) {
    String cmd = SerialBT.readStringUntil('\n');
    cmd.trim();
    cmd.toLowerCase();  // biar case-insensitive

    if (cmd == "start") {
      Serial.println(">> RECORD ON");
      digitalWrite(RecordON, HIGH);
      isRecording = true;
    }
    else if (cmd == "stop") {
      Serial.println(">> RECORD OFF");
      digitalWrite(RecordON, LOW);
      isRecording = false;
    }
    else if (isRecording && cmd == "success") {
      Serial.println(">> HIGHLIGHT SUCCESS");
      digitalWrite(HighlightInd, HIGH);
      delay(1000);
      digitalWrite(HighlightInd, LOW);
    }
  }

  delay(20); // debounce sederhana
}
