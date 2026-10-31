"""Hand-written chat-style samples for benchmarking scam-text models.

SYNTHETIC: written by the assistant, not collected from real chats, so a result
here is a first screen and not an accuracy figure. ``hard`` marks legitimate
messages that share vocabulary with scams (OTPs, urgent requests, "my new
number", promos, money between friends) and scams that are short or polite.

Same tuple shape as email_samples.SAMPLES: (id, label, hard, subject, body),
with an empty subject so the model sees just the message.
"""

from __future__ import annotations

LEGIT = "legit"
PHISH = "phish"  # "scam" here; the label name is shared with the email samples

SAMPLES: list[tuple[str, str, bool, str, str]] = [
    # ---------------------------------------------------------------- legit
    ("CL01", LEGIT, False, "", "ok see you at 7, bring the charger"),
    ("CL02", LEGIT, True, "", "Can you call me urgently? Mom is fine, I just need you to pick her up from the clinic before 5."),
    ("CL03", LEGIT, True, "", "Your OTP for the food order is 4821. Do not share it with anyone."),
    ("CL04", LEGIT, True, "", "Hey did you get the money I sent for the rent? Check your UPI and tell me."),
    ("CL05", LEGIT, False, "", "Happy birthday!! Hope you have an amazing day, dinner is on me"),
    ("CL06", LEGIT, False, "", "Dinner at 8? I'll book the place near the station."),
    ("CL07", LEGIT, True, "", "Hi, this is Raj from the plumbing service. I'll be at your place tomorrow between 10 and 12."),
    ("CL08", LEGIT, True, "", "Hi Mum, I reached safely. Phone battery is low, will call you tomorrow morning."),
    ("CL09", LEGIT, False, "", "Don't forget the society meeting tonight at 9 in the clubhouse"),
    ("CL10", LEGIT, True, "", "Offer ends today! Flat 40% off on all shoes at our store. Show this message at the counter."),
    ("CL11", LEGIT, True, "", "Your package has been delivered to the reception. Please rate your experience."),
    ("CL12", LEGIT, False, "", "Can you send me the notes from class? I missed it yesterday"),
    ("CL13", LEGIT, False, "", "Bro the match is starting, hurry up"),
    ("CL14", LEGIT, True, "", "This is my new number, I changed my SIM last week. Please save it."),
    ("CL15", LEGIT, True, "", "Reminder: your appointment is tomorrow at 11:30. Reply YES to confirm or call to reschedule."),
    ("CL16", LEGIT, True, "", "Can you transfer the 500 I lent you last week? No rush at all."),
    ("CL17", LEGIT, False, "", "Here is the doc for tomorrow's meeting, please review it before 10"),
    ("CL18", LEGIT, True, "", "Congratulations on the new job!! We have to celebrate, treat soon?"),
    ("CL19", LEGIT, False, "", "Good morning everyone, have a great week ahead"),
    ("CL20", LEGIT, True, "", "Please verify your email to activate your library account. The code is 553201."),
    # ---------------------------------------------------------------- scam
    ("CP01", PHISH, False, "", "Congratulations! You have won Rs 25,00,000 in the KBC lottery. Contact our agent on this number to claim your prize."),
    ("CP02", PHISH, False, "", "Hi Mum, I lost my phone. This is my new number. Can you send 15000 urgently? I will explain later."),
    ("CP03", PHISH, False, "", "Part-time job: earn 3000 per day working from home. Just like and subscribe to videos. Message us to start."),
    ("CP04", PHISH, False, "", "Your bank KYC expired today. Click the link to update your PAN and avoid account block."),
    ("CP05", PHISH, False, "", "Dear customer your electricity will be disconnected tonight at 9:30 pm. Call the officer immediately on this number."),
    ("CP06", PHISH, False, "", "Hello, I am from Amazon HR. We have a work from home opportunity paying 5000 daily. Contact our manager."),
    ("CP07", PHISH, False, "", "Invest 10000 today and get 50000 in one week. Guaranteed returns from our trading team. Join the group."),
    ("CP08", PHISH, False, "", "Your parcel is held at customs. Pay the 49 rupee fee at the link to release it."),
    ("CP09", PHISH, False, "", "I am stuck abroad and my wallet was stolen. Please send money urgently, I will return it once I am home."),
    ("CP10", PHISH, False, "", "Sir your SIM will be blocked in 2 hours. Verify your Aadhaar at this link now."),
    ("CP11", PHISH, False, "", "Hi dear, I saw your profile. I am a soldier overseas and need help with shipping my funds."),
    ("CP12", PHISH, False, "", "You have been selected for the lucky draw. Pay a small delivery charge to receive your new phone."),
    ("CP13", PHISH, True, "", "Please scan this QR code to receive your refund of Rs 5000."),
    ("CP14", PHISH, True, "", "Hello this is your relative, my number changed, can you help with a payment today? Please do not tell anyone."),
    ("CP15", PHISH, False, "", "Your credit card reward points expire today. Redeem now at the link: http://bit.ly/x7Yz"),
    ("CP16", PHISH, True, "", "Hey are you free? I need a small favour, can you buy some gift cards for me? I will pay you back tonight."),
    ("CP17", PHISH, False, "", "Did you apply for a loan? Instant approval, no documents, just send Rs 2000 processing fee."),
    ("CP18", PHISH, False, "", "Your WhatsApp will expire. Forward this message to 10 people or your account will be banned."),
    ("CP19", PHISH, True, "", "Hello sir, I found your lost wallet. Please share the OTP you just received to verify you are the owner."),
    ("CP20", PHISH, False, "", "Earn money online. Rate hotels and get paid 800 per task. Deposit to unlock higher-paying tasks."),
]


def as_text(sample: tuple[str, str, bool, str, str]) -> str:
    """The text a model sees: the message."""
    return sample[4]
