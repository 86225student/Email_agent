import os
import smtplib
import imaplib
import email
import time
import logging
import sys
from email.message import EmailMessage
from email.header import decode_header
from email.utils import parseaddr

import google.generativeai as genai

# --- Konfiguracja ---

# Konfiguracja logowania - Render będzie zbierał te logi
logging.basicConfig(level=logging.INFO, 
                    format='%(asctime)s - %(levelname)s - %(message)s',
                    stream=sys.stdout)

# Wczytaj dane logowania i klucz API ze zmiennych środowiskowych
# Ustawisz je w panelu Render w zakładce "Environment"
try:
    GEMINI_API_KEY = os.environ['GEMINI_API_KEY']
    EMAIL_ADDRESS = os.environ['EMAIL_ADDRESS']
    EMAIL_PASSWORD = os.environ['EMAIL_PASSWORD']
except KeyError as e:
    logging.fatal(f"BŁĄD: Brak kluczowej zmiennej środowiskowej: {e}")
    logging.fatal("Upewnij się, że GEMINI_API_KEY, EMAIL_ADDRESS, i EMAIL_PASSWORD są ustawione w Render.")
    sys.exit(1) # Zakończ pracę, jeśli brakuje konfiguracji

# Ustawienia serwerów pocztowych (dla Gmaila)
IMAP_SERVER = "imap.gmail.com"
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 465  # Port SSL dla SMTP

# Jak często sprawdzać nowe maile (w sekundach)
CHECK_INTERVAL_SEC = 60

# --- Logika Gemini ---

def get_gemini_response(prompt):
    """Wysyła prompt do Gemini i zwraca odpowiedź."""
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel('gemini-pro')
        response = model.generate_content(prompt)
        
        # Sprawdzenie, czy odpowiedź nie została zablokowana
        if not response.parts:
            logging.warning("Odpowiedź Gemini była pusta lub zablokowana (safety reasons).")
            return "Niestety, nie mogę wygenerować odpowiedzi na ten temat (odpowiedź zablokowana)."
            
        return response.text
    except Exception as e:
        logging.error(f"Błąd podczas komunikacji z API Gemini: {e}")
        return None # Zwróć None, aby nie wysyłać odpowiedzi

# --- Logika E-mail ---

def parse_email_body(msg):
    """Próbuje wyciągnąć "czystą" treść (prompt) z wiadomości e-mail."""
    # Priorytet to "text/plain"
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            cdispo = str(part.get("Content-Disposition"))

            # Szukamy części "text/plain", która nie jest załącznikiem
            if ctype == "text/plain" and "attachment" not in cdispo:
                try:
                    # Dekodujemy treść używając charsetu z maila lub 'utf-8'
                    charset = part.get_content_charset() or 'utf-8'
                    return part.get_payload(decode=True).decode(charset, errors='ignore')
                except Exception as e:
                    logging.warning(f"Błąd dekodowania części maila: {e}")
                    # Spróbuj domyślnego dekodowania
                    return part.get_payload(decode=True).decode(errors='ignore')
    else:
        # Mail nie jest multipart, bierzemy główną treść
        try:
            charset = msg.get_content_charset() or 'utf-8'
            return msg.get_payload(decode=True).decode(charset, errors='ignore')
        except Exception as e:
            logging.warning(f"Błąd dekodowania maila (non-multipart): {e}")
            return None
    
    return None # Nie znaleziono pasującej treści

def send_reply(to_address, subject, original_msg_id, body):
    """Wysyła wiadomość e-mail jako odpowiedź (z zachowaniem wątku)."""
    logging.info(f"Przygotowuję odpowiedź do: {to_address}")
    
    # Tworzenie obiektu wiadomości
    msg = EmailMessage()
    msg['From'] = EMAIL_ADDRESS
    msg['To'] = to_address
    msg['Subject'] = f"Re: {subject}"
    
    # Te nagłówki są KLUCZOWE, aby klient poczty rozpoznał to jako odpowiedź
    msg['In-Reply-To'] = original_msg_id
    msg['References'] = original_msg_id
    
    msg.set_content(body)

    # Logowanie do serwera SMTP i wysyłka
    try:
        with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT) as smtp:
            smtp.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
            smtp.send_message(msg)
        logging.info(f"Odpowiedź wysłana pomyślnie do {to_address}.")
    except Exception as e:
        logging.error(f"Nie udało się wysłać e-maila: {e}")

def decode_subject(subject):
    """Poprawnie dekoduje temat e-maila (który może być w różnych formatach)."""
    decoded_parts = decode_header(subject)
    subject_str = ""
    for part, charset in decoded_parts:
        if isinstance(part, bytes):
            subject_str += part.decode(charset or 'utf-8', errors='ignore')
        else:
            subject_str += part
    return subject_str.strip() or "Brak tematu"

def check_emails():
    """Główna funkcja sprawdzająca i przetwarzająca nowe e-maile."""
    logging.info("Łączenie z serwerem IMAP...")
    
    try:
        mail = imaplib.IMAP4_SSL(IMAP_SERVER)
        mail.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
        mail.select("inbox")
        logging.info("Połączono. Sprawdzam nieprzeczytane wiadomości...")

        # Wyszukaj tylko nieprzeczytane wiadomości
        status, data = mail.search(None, "UNSEEN")
        if status != "OK":
            logging.error("Nie udało się przeszukać skrzynki.")
            mail.logout()
            return

        mail_ids = data[0].split()
        if not mail_ids:
            logging.info("Brak nowych wiadomości.")
            mail.logout()
            return

        logging.info(f"Znaleziono {len(mail_ids)} nowych wiadomości.")

        for mail_id in mail_ids:
            # Pobierz pełną wiadomość
            status, msg_data = mail.fetch(mail_id, "(RFC822)")
            if status != "OK":
                logging.warning(f"Nie udało się pobrać maila ID: {mail_id}")
                continue

            # Parsuj wiadomość
            raw_email = msg_data[0][1]
            msg = email.message_from_bytes(raw_email)

            # Pobierz kluczowe informacje
            sender_email = parseaddr(msg['From'])[1] # Czysty adres e-mail nadawcy
            subject = decode_subject(msg['Subject'])
            msg_id = msg['Message-ID'] # Ważne dla odpowiedzi w wątku

            # --- GŁÓWNA LOGIKA AGENTA ---
            
            # 1. Sprawdź, czy to nie jest mail od nas samych (ważne!)
            if sender_email == EMAIL_ADDRESS:
                logging.info(f"Pominięto maila od samego siebie (ID: {mail_id}).")
            else:
                logging.info(f"Przetwarzam maila od: {sender_email}, Temat: {subject}")
                
                # 2. Wyciągnij prompt z treści
                prompt = parse_email_body(msg)

                if not prompt:
                    logging.warning(f"Nie znaleziono treści (text/plain) w mailu ID: {mail_id}.")
                    # I tak oznaczamy jako przeczytany
                else:
                    # 3. Wykonaj prompt w Gemini
                    logging.info("Wysyłam prompt do Gemini...")
                    gemini_answer = get_gemini_response(prompt.strip())

                    if gemini_answer:
                        # 4. Odeślij odpowiedź
                        send_reply(sender_email, subject, msg_id, gemini_answer)
                    else:
                        logging.error(f"Nie udało się uzyskać odpowiedzi Gemini dla maila ID: {mail_id}.")

            # 5. Oznacz mail jako przeczytany (Seen)
            # Robimy to niezależnie od tego, czy się udało, aby nie utknąć
            mail.store(mail_id, '+FLAGS', r'(\Seen)')

        mail.logout()

    except Exception as e:
        logging.error(f"Wystąpił błąd w głównej funkcji check_emails: {e}", exc_info=True)


# --- Główna pętla agenta ---

if __name__ == "__main__":
    logging.info("Agent AI startuje...")
    logging.info("Sprawdzanie poczty będzie powtarzane co 60 sekund.")
    
    while True:
        try:
            check_emails()
        except Exception as e:
            logging.critical(f"Krytyczny błąd w głównej pętli: {e}", exc_info=True)
            # W przypadku krytycznego błędu (np. utrata połączenia)
            # i tak spróbujemy ponownie po przerwie
        
        logging.info(f"Odpoczywam przez {CHECK_INTERVAL_SEC} sekund...")
        time.sleep(CHECK_INTERVAL_SEC)
