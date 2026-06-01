# E-Mail-Mapping: Exchange-Adresse → Stalwart-Adresse
# Hier weitere Sonderfälle eintragen falls nötig
EMAIL_MAP = {
    # 'exchange-address@domain.com': 'stalwart-address@domain.com',
}

def stw_email(exchange_email):
    """Gibt die Stalwart-E-Mail für eine Exchange-E-Mail zurück"""
    return EMAIL_MAP.get(exchange_email, exchange_email)
