// prompts.js - the quick-test scenario texts (presets) used by the "Quick Test Scenarios" buttons.
//
// (The former "GET PROMPT" task bar was replaced by the Scam Example Lab, see examples.js.)
// Presets are plain sample messages; this module has no behaviour and makes no network calls.

export const PRESETS = {
  digital_arrest: {
    label: 'Digital Arrest Threat',
    text: 'URGENT NOTICE: This is Inspector Sharma from the CBI Cyber Crime Department. A parcel linked to your Aadhaar was seized by Customs containing illegal items. An arrest warrant has been issued in your name. You are now under digital arrest. Do not tell anyone or disconnect this call, and transfer Rs 85,000 via UPI within 24 hours to the RBI verification account to clear your case immediately.',
  },
  bank_otp: {
    label: 'Bank KYC & OTP Trap',
    text: 'Dear Customer, your bank account will be frozen today due to incomplete KYC. Our bank officer requires you to click this link immediately and enter your One-Time Password (OTP) and PIN to prevent permanent suspension.',
  },
  voice_clone: {
    label: 'AI CEO Urgent Wire',
    text: 'Hey, I am stuck in a confidential board meeting and cannot take a normal call. Keep this secret from finance for now. I need you to transfer money via crypto or gift card to our new vendor immediately—the audio sounded slightly robotic like a synthetic voice or scripted recording, and they rushed me to send bank details within 10 minutes.',
  },
  safe_msg: {
    label: 'Routine Delivery Update',
    text: 'Hi! Your package from BookStore will arrive between 2 PM and 5 PM tomorrow. No payment is due on delivery. You can track the shipment anytime directly inside your order history in the official app.',
  },
};
