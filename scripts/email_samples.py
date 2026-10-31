"""Hand-written sample emails for benchmarking phishing-text models.

These are SYNTHETIC: written by the assistant, not collected from real mail.
They are useful for a first screen (does a model separate obvious phishing from
ordinary mail, and does it false-alarm on tricky legitimate mail?) and are NOT
evidence of accuracy on real traffic. Real mail is messier and its phishing is
more varied. Replace or extend with your own samples before trusting a model.

``hard`` marks the samples chosen to be difficult:
  * legitimate mail that shares vocabulary with phishing (OTP codes, "sale ends
    tonight", password resets, payment notices, urgent-but-genuine requests);
  * phishing that is polite, short or avoids the usual pressure words.

All domains are fictional. Each sample is (id, label, hard, subject, body).
"""

from __future__ import annotations

LEGIT = "legit"
PHISH = "phish"

SAMPLES: list[tuple[str, str, bool, str, str]] = [
    # ---------------------------------------------------------------- legit
    ("L01", LEGIT, False, "Notes from this morning",
     "Hi Anil, thanks for the call this morning. As discussed, I've attached the revised project timeline. "
     "Could you review it and send comments by Thursday? Best, Meera"),
    ("L02", LEGIT, False, "Lunch on Friday?",
     "Are you free for lunch on Friday? There's a new place near the office that does great thali. "
     "Let me know and I'll book a table for four."),
    ("L03", LEGIT, True, "This week in tech",
     "This week: a look at open-source databases, three tips for faster builds, and an interview with a "
     "maintainer. Read more on our blog. You are receiving this because you subscribed. Unsubscribe any time."),
    ("L04", LEGIT, True, "Our end-of-season sale ends tonight",
     "Our end-of-season sale ends tonight! Take 30% off everything in store and online. Use code SAVE30 at "
     "checkout. Free delivery on orders over 999."),
    ("L05", LEGIT, True, "Your one-time password",
     "Your one-time password for the transaction of INR 2,500 is 481920. It is valid for 10 minutes. Do not "
     "share this code with anyone. If you did not request this, call the number on the back of your card."),
    ("L06", LEGIT, True, "Your order has shipped",
     "Your order #84731 has shipped and will arrive on Friday. You can track your parcel from your account "
     "page. Thanks for shopping with us."),
    ("L07", LEGIT, True, "Reset your password",
     "We received a request to reset your password. If you made this request, use the link below within 1 "
     "hour. If you did not, you can safely ignore this email and your password will stay the same."),
    ("L08", LEGIT, True, "New sign-in to your account",
     "We noticed a new sign-in to your account from Chrome on Windows. If this was you, no action is needed. "
     "If not, review your recent activity in your account settings."),
    ("L09", LEGIT, True, "Invoice 2291 for October",
     "Please find attached invoice 2291 for October consulting services, payable within 30 days to the "
     "account on our standard terms. Let me know if you have any questions."),
    ("L10", LEGIT, False, "Updated leave policy",
     "Reminder: the updated leave policy takes effect on 1 November. The full document is on the intranet. "
     "Please reach out to HR with any questions."),
    ("L11", LEGIT, False, "Invitation: Quarterly review",
     "You are invited to the Quarterly review on Tuesday, 10:00 to 11:00. Join using the meeting link in the "
     "calendar event. Please add agenda items to the shared document beforehand."),
    ("L12", LEGIT, True, "Receipt for your payment",
     "Thank you for your payment of $49.00 to Acme Cloud. Your receipt is attached. Your subscription renews "
     "on 12 December and you can cancel any time from your billing settings."),
    ("L13", LEGIT, False, "We received your application",
     "Thank you for applying for the Backend Engineer role. We have received your application and will be "
     "in touch within two weeks if your profile matches."),
    ("L14", LEGIT, False, "Call me",
     "Hi beta, call me when you're free. Your aunt is visiting this weekend and wants to see you. "
     "Don't forget to bring the photos from the trip."),
    ("L15", LEGIT, False, "Comment on pull request #214",
     "Alex commented on pull request #214: Looks good, but can you add a test for the empty-input case? "
     "Once that's in I'm happy to approve."),
    ("L16", LEGIT, False, "Your flight to Pune is confirmed",
     "Your flight to Pune on 14 November is confirmed. Booking reference QX7R2P. Online check-in opens 48 "
     "hours before departure. Have a pleasant trip."),
    ("L17", LEGIT, True, "Your October statement is ready",
     "Your October statement is ready. Sign in to the app to view it. The minimum payment due is shown on the "
     "statement, and the due date is 5 November."),
    ("L18", LEGIT, False, "Exam timetable published",
     "Your exam timetable has been published on the student portal. Please check your slots and report any "
     "clashes to the department office by Friday."),
    ("L19", LEGIT, False, "Re: Sync issue on your account",
     "Thanks for contacting support. We've reset the configuration on your account and the sync should work "
     "now. Please let us know if the issue persists."),
    ("L20", LEGIT, True, "Payslip for October",
     "Your payslip for October is available in the HR portal. Contact the payroll team if you notice any "
     "discrepancies in the figures."),
    ("L21", LEGIT, False, "Membership renewal",
     "Your gym membership renews next week. No action is needed. To change your plan, visit the front desk "
     "or use the app."),
    ("L22", LEGIT, False, "Society meeting Sunday",
     "The society meeting is on Sunday at 6 pm in the clubhouse. Agenda: water tank cleaning and parking "
     "allocation. Please try to attend."),
    ("L23", LEGIT, True, "Tax documents needed",
     "Reminder from your accountant: please send your Form 16 and investment proofs by 15 December so we can "
     "file your return on time. A scanned copy by email is fine."),
    ("L24", LEGIT, True, "Verify your email",
     "Welcome aboard! Your verification code is 552901. It expires in 15 minutes. If you didn't create an "
     "account, you can ignore this message."),
    ("L25", LEGIT, False, "Appointment reminder",
     "This is a reminder of your appointment with Dr Rao tomorrow at 4:30 pm. Reply C to confirm or call the "
     "clinic to reschedule."),
    ("L26", LEGIT, False, "Version 3.2 released",
     "We've released version 3.2 with faster search and a new dark mode. See the release notes for details "
     "and known issues."),
    ("L27", LEGIT, True, "Demo server is down",
     "Hi team, urgent: the demo server is down and the client call starts in an hour. Can someone from infra "
     "look at it right now? I'm in the meeting but can join a call after."),
    ("L28", LEGIT, True, "Your refund has been processed",
     "Your refund of $18.50 has been processed and should appear in your account within 5 business days. "
     "No further action is required."),
    ("L29", LEGIT, True, "Important: changes to our terms",
     "Important: we are updating our terms of service effective 1 January. No action is required from you. "
     "You can read the changes on our website."),
    ("L30", LEGIT, False, "Thank you",
     "Thank you so much for helping with the move last weekend. We couldn't have done it without you. "
     "Dinner is on us next time."),
    # ---------------------------------------------------------------- phish
    ("P01", PHISH, False, "Account suspended",
     "Dear customer, your account has been suspended due to unusual activity. Verify your identity "
     "immediately by clicking the link below or your account will be permanently closed within 24 hours."),
    ("P02", PHISH, False, "KYC expired - account will be blocked",
     "Dear user, your bank KYC has expired and your account will be blocked today. Update your PAN and Aadhaar "
     "details now at the link below to avoid suspension."),
    ("P03", PHISH, False, "Quick favour",
     "Hi, are you at your desk? I need you to buy 5 gift cards of $100 for clients. It's urgent and "
     "confidential, I'm in a meeting and can't talk. I'll reimburse you. Send me the codes as soon as you have them."),
    ("P04", PHISH, True, "Updated invoice 4471 - new bank details",
     "Please note our bank details have changed. Kindly make all future payments to the new account below. "
     "Attached is the updated invoice 4471; payment is due today."),
    ("P05", PHISH, False, "Parcel on hold",
     "Your parcel could not be delivered because of an unpaid customs fee of $2.99. Pay now to schedule "
     "redelivery, otherwise it will be returned to the sender."),
    ("P06", PHISH, False, "Tax refund available",
     "You are eligible for a tax refund of $842.60. To receive it, confirm your bank details through the "
     "secure form below within 48 hours."),
    ("P07", PHISH, False, "Exclusive investment invitation",
     "Congratulations! You have been selected to join our exclusive crypto investment group with guaranteed "
     "daily returns of 5%. Deposit just $200 today to start earning."),
    ("P08", PHISH, True, "Password expires today",
     "Your Microsoft 365 password expires today. Click below to keep the same password and avoid losing "
     "access to your mailbox."),
    ("P09", PHISH, True, "Document shared for signature",
     "A document has been shared with you for review and signature. Sign in with your email password to view "
     "the document."),
    ("P10", PHISH, False, "Remote job offer",
     "We reviewed your resume and would like to offer you a remote data entry job paying $450 per day. No "
     "experience needed. Send us your bank account details so we can set up your salary."),
    ("P11", PHISH, False, "Inheritance claim",
     "I am a barrister representing the late Mr Williams who left $8.5 million. As you share his surname I "
     "would like to present you as next of kin. Reply with your full name and phone number to proceed."),
    ("P12", PHISH, False, "You have won",
     "Your email has won $1,000,000 in the international online lottery. To claim your prize, contact our "
     "agent with your name, address and a processing fee."),
    ("P13", PHISH, True, "Payment of $349.99 detected",
     "We noticed a payment of $349.99 to Crypto Exchange from your account. If you did not authorise this, "
     "log in now to dispute it and confirm your card number."),
    ("P14", PHISH, False, "Payment declined",
     "Your payment method was declined and your membership will end today. Update your card details now to "
     "continue watching."),
    ("P15", PHISH, True, "Confirming payment account",
     "Hello, I hope you are well. Could you kindly confirm the account number you would like us to use for "
     "this month's payment? We want to be sure the funds go to the right place."),
    ("P16", PHISH, True, "Direct deposit update",
     "Hi, I would like to update my direct deposit details before the next payroll. Please change my account "
     "to the new one below and confirm by return."),
    ("P17", PHISH, False, "Mailbox almost full",
     "Your mailbox is 98% full. Click here to increase your storage quota or you will stop receiving new "
     "messages."),
    ("P18", PHISH, False, "Mailbox migration",
     "This is the IT helpdesk. We are migrating mailboxes this weekend. Reply with your username and password "
     "so your account can be moved without losing data."),
    ("P19", PHISH, True, "Subscription renewed",
     "Your antivirus subscription of $399.99 has been renewed. If you did not authorise this charge, call our "
     "support line immediately for a refund."),
    ("P20", PHISH, False, "Urgent wire transfer",
     "I need you to process an urgent wire transfer of $18,400 to a new supplier today. I'm travelling so "
     "email only. Let me know when it's done."),
    ("P21", PHISH, False, "Copyright violation notice",
     "Your account violated our copyright policy. Submit the appeal form within 24 hours or your page will "
     "be deleted."),
    ("P22", PHISH, True, "Confidential bonus statement",
     "Please review the attached confidential bonus statement for this year. Sign in with your company "
     "credentials to open the file."),
    ("P23", PHISH, False, "Delivery attempt failed",
     "We attempted to deliver your package but the address was incomplete. Update your address at the link "
     "to avoid return."),
    ("P24", PHISH, False, "Hello from overseas",
     "I saw your profile and felt a connection. I am an engineer working overseas and need help with a small "
     "transfer fee to come home and meet you."),
    ("P25", PHISH, False, "Flood victims need help",
     "Help victims of the flood. Send donations in gift cards or crypto to the address below; every minute "
     "counts."),
    ("P26", PHISH, False, "Apple ID locked",
     "We detected suspicious activity on your Apple ID. Your account has been locked. Verify your Apple ID "
     "and card details to unlock it."),
    ("P27", PHISH, False, "Final notice: income tax",
     "Final notice from the Income Tax Department: unpaid dues of Rs 38,000 will lead to legal action. Pay "
     "immediately via the link to avoid arrest."),
    ("P28", PHISH, True, "Domain renewal invoice",
     "Invoice attached for domain renewal of $289. If you do not wish to renew, call to cancel; otherwise "
     "payment will be charged automatically."),
    ("P29", PHISH, True, "File shared: Q4_Salary_Review.xlsx",
     "John shared a file with you: Q4_Salary_Review.xlsx. Click to open with your email credentials."),
    ("P30", PHISH, False, "Part-time job",
     "Hi, I'm from HR. We have a part-time job, earn 3000 per day by liking videos. Contact our manager on "
     "WhatsApp to get started."),
]


def as_text(sample: tuple[str, str, bool, str, str]) -> str:
    """The text a model sees: subject then body."""
    return f"{sample[3]}\n{sample[4]}"
