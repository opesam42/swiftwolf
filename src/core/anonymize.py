"""Deterministic beneficiary-name anonymization, shared by both the
SwiftWolf-side seed job (jobs/seed_customer_profile.py) and the
bankapp-seed-data path (OnboardingService.get_bankapp_seed_rows) — the single
function both call so a given beneficiary always gets the SAME fabricated
name on both sides, never two different stories about the same fake person.

Only the name gets masked. Account numbers and bank codes are already
fabricated-looking, non-identifying strings on their own — the actual privacy
concern is specifically a real human name being visible in seed data, which is
the one field worth anonymizing.
"""
import hashlib

# 150 entries, not 20 — a small pool means many distinct beneficiaries hash
# onto the same handful of names, which looks obviously synthetic. A larger
# pool spreads collisions out enough that repeated names look like plausible
# coincidence instead of a giveaway.
FABRICATED_NAMES = [
    # Yoruba
    "Adebayo Ogunleye", "Folake Adeyemi", "Segun Ojo", "Bisi Afolabi", "Kunle Adebayo",
    "Tunde Bakare", "Yemi Owolabi", "Bimbo Fashola", "Wale Adeyinka", "Damilola Ajayi",
    "Kemi Oyelaran", "Femi Sowande", "Titi Adebisi", "Gbenga Alabi", "Ronke Fagbenle",
    "Sola Ogundipe", "Bode Akintola", "Yinka Balogun", "Toyin Oyewole", "Dele Adegoke",
    "Funmi Ariyo", "Lekan Odutola", "Bukola Fatoki", "Seun Adewumi", "Wumi Ogunbanjo",
    "Ayo Fagbemi", "Ola Adeleke", "Tayo Akinyemi", "Remi Sanni", "Kayode Ilori",
    # Igbo
    "Adaeze Okafor", "Chidi Nwosu", "Ngozi Eze", "Emeka Obi", "Amaka Chukwu",
    "Ifeoma Nnamdi", "Obinna Eze", "Chiamaka Uche", "Ikenna Anyanwu", "Uche Okonkwo",
    "Chinedu Okoro", "Ebele Nwachukwu", "Kelechi Okereke", "Adaobi Umeh", "Chukwuemeka Iwu",
    "Nkechi Onyekwere", "Chibuzo Ugwu", "Chinwe Okpara", "Ekene Nwankwo", "Onyinye Madu",
    "Chukwudi Eze", "Adanna Igwe", "Ifeanyi Obasi", "Nneka Okoli", "Ejike Anozie",
    "Chiedozie Nwabueze", "Amarachi Nduka", "Ugonna Ezenwa", "Chukwuka Oti", "Obioma Ejiofor",
    # Hausa / Fulani
    "Aisha Bello", "Musa Ibrahim", "Fatima Sani", "Yusuf Abubakar", "Zainab Umar",
    "Aliyu Garba", "Hauwa Yakubu", "Nasir Adamu", "Amina Suleiman", "Bashir Lawal",
    "Hadiza Mohammed", "Sani Usman", "Rukayya Danjuma", "Isah Shehu", "Maryam Tijjani",
    "Abdullahi Bala", "Halima Musa", "Ahmad Sadiq", "Safiya Nuhu", "Idris Kabir",
    "Jamila Aliyu", "Umar Faruk", "Rabiu Auwal", "Salamatu Yahaya", "Ismail Haruna",
    "Khadija Abdullahi", "Suleiman Rufai", "Balaraba Umaru", "Garba Muhammad", "Zulaihat Ahmed",
    # Edo / Delta / South-South
    "Osaze Ogbebor", "Efe Omoregie", "Osato Aigbe", "Enivwenae Okoduwa", "Ese Aikhomu",
    "Osaretin Igbinovia", "Ivie Aghedo", "Osayi Uwadia", "Ovie Erhabor", "Efosa Idahosa",
    "Blessing Ogboru", "Preye Amachree", "Tamuno Wonodi", "Ibiere Sokari", "Kimiebi Ombu",
    "Ere-ebi Bekederemo", "Diepiriye Fubara", "Sylva Amakiri", "Suoyo Amachree", "Doubra Kalio",
    # Mixed / other
    "Grace Adamu", "Peter Nwachukwu", "John Okafor", "Mary Adeleke", "David Eze",
    "Ruth Balogun", "Samuel Ibrahim", "Esther Okonkwo", "Daniel Ogunleye", "Comfort Nnamdi",
    "Victor Adeyemi", "Patience Chukwu", "Michael Bello", "Blessing Okoro", "James Adebayo",
    "Joy Nwosu", "Emmanuel Sani", "Faith Uche", "Joseph Umar", "Precious Eze",
    "Anthony Okoye", "Gift Adamu", "Paul Nwankwo", "Mercy Yakubu", "Stephen Obi",
    "Christiana Musa", "Francis Chukwuemeka", "Ada Suleiman", "Simon Igwe", "Beauty Anyanwu",
    "Matthew Okereke", "Deborah Garba", "Charles Nnaji", "Glory Umeh", "Richard Aliyu",
    "Peace Nwabueze", "Andrew Okpara", "Favour Ogbonna", "Vincent Danjuma", "Blessing Madu",
]


def anonymize_name(beneficiary_account: str, beneficiary_bank_code: str) -> str:
    """Deterministic: the same (account, bank_code) composite key always picks
    the same fabricated name from the pool — same hash, same index, every
    time, on both sides of the pipeline."""
    key = f"{beneficiary_account}:{beneficiary_bank_code}"
    digest = hashlib.sha256(key.encode()).hexdigest()
    index = int(digest, 16) % len(FABRICATED_NAMES)
    return FABRICATED_NAMES[index]
