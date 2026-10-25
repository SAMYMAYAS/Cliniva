import base64
from datetime import date, datetime
import json
import logging
import os
from pathlib import Path
import platform
import subprocess
import time
from typing import Dict, Any, List

from fastapi import APIRouter, HTTPException
from database import DOCS_DIR, get_db_cursor

logger = logging.getLogger("PatientsModule")

patients_router = APIRouter(prefix="/api/patients-module", tags=["Gestion des Patients"])

# Répertoire officiel des 58 Wilayas et communes d'Algérie
ALGERIA_WILAYAS = {
    "01 - Adrar": [
        "Adrar",
        "Bouda",
        "Ouled Ahmed Timmi",
        "Reggane",
        "Sali",
        "Sebaa",
        "Tsabit",
        "In Zghmir",
        "Zaouiet Kounta",
        "Akabli",
        "Aoulef",
        "Timekten",
        "Tit",
        "Fenoughil",
        "Tamantit",
        "Tamest",
    ],
    "02 - Chlef": [
        "Chlef",
        "Oum Drou",
        "Sendjas",
        "Sidi Abderrahmane",
        "Sidi Akkacha",
        "Tenes",
        "Beni Bouattab",
        "El Karimia",
        "Harchoun",
        "Dahra",
        "Taougrit",
        "Beni Haoua",
        "Breira",
        "Oued Goussine",
        "Chettia",
        "Labiod Medjadja",
        "Ouled Fares",
        "Boukadir",
        "Oued Sly",
        "Sobha",
        "Benairia",
        "Bouzeghaia",
        "Zeboudja",
        "Abou El Hassane",
        "Tadjena",
        "Talassa",
        "El Marsa",
        "Moussadek",
        "Beni Rached",
        "Oued Fodda",
        "Ouled Abbes",
        "El Hadjadj",
        "Ouled Ben Abdelkader",
        "Ain Merane",
        "Herenfa",
    ],
    "03 - Laghouat": [
        "Laghouat",
        "Benacer Benchohra",
        "Ksar El Hirane",
        "El Assafia",
        "Sidi Makhlouf",
        "Hassi Delaa",
        "Hassi R'mel",
        "Ain Madhi",
        "El Haouaita",
        "Kheneg",
        "Tadjemout",
        "Tadjrouna",
        "Ain Sidi Ali",
        "El Beidha",
        "Gueltat Sidi Saad",
        "Brida",
        "Hadj Mechri",
        "Taouiala",
        "El Ghicha",
        "Aflou",
        "Sebgag",
        "Sidi Bouzid",
        "Oued Morra",
        "Oued M'zi",
        "Ain Zitoun",
    ],
    "04 - Oum El Bouaghi": [
        "Oum El Bouaghi",
        "Ain Beida",
        "Berriche",
        "Zorg",
        "Ain M'lila",
        "Ouled Gacem",
        "Ouled Hamla",
        "El Amiria",
        "Sigus",
        "Ain Babouche",
        "Ain Diss",
        "Dhalaa",
        "El Djazia",
        "Ain Kercha",
        "El Harmilia",
        "Hanchir Toumghani",
        "Fkirina",
        "Oued Nini",
        "Bir Chouhada",
        "Ouled Zouai",
        "Souk Naamane",
        "Ksar Sbahi",
        "Behir Chergui",
        "El Belala",
        "Meskiana",
        "Rahia",
        "Ain Fekroun",
        "El Fedjoudj Boughrara Sa",
    ],
    "05 - Batna": [
        "Batna",
        "Fesdis",
        "Oued Chaaba",
        "Hidoussa",
        "Ksar Bellezma",
        "Merouana",
        "Oued El Ma",
        "Lazrou",
        "Seriana",
        "Zanet El Beida",
        "Menaa",
        "Tigharghar",
        "Ain Yagout",
        "Boumia",
        "Djerma",
        "El Madher",
        "Ouyoun El Assafir",
        "Tazoult",
        "Boumagueur",
        "N Gaous",
        "Sefiane",
        "Arris",
        "Tighanimine",
        "Ain Djasser",
        "El Hassi",
        "Seggana",
        "Tilatou",
        "Foum Toub",
        "Ichemoul",
        "Inoughissen",
        "Bouzina",
        "Larbaa",
        "Boulhilat",
        "Chemora",
        "Barika",
        "Bitam",
        "M Doukal",
        "Azil Abedelkader",
        "Djezzar",
        "Ouled Ammar",
        "Ghassira",
        "Kimmel",
        "T Kout",
        "Ain Touta",
        "Beni Foudhala El Hakania",
        "Maafa",
        "Ouled Aouf",
        "Chir",
        "Oued Taga",
        "Teniet El Abed",
        "Ouled Fadel",
        "Timgad",
        "Gosbat",
        "Guigba",
        "Ouled Sellem",
        "Rahbat",
        "Ras El Aioun",
        "Talkhamt",
        "Lemcene",
        "Ouled Si Slimane",
        "Taxlent",
    ],
    "06 - Béjaïa": [
        "Bejaia",
        "Oued Ghir",
        "Amizour",
        "Beni Djellil",
        "Feraoun",
        "Smaoun",
        "Timezrit",
        "Melbou",
        "Souk El Tenine",
        "Tamridjet",
        "Boukhelifa",
        "Tala Hamza",
        "Tichy",
        "Ait R'zine",
        "Ighil-Ali",
        "Ait-Smail",
        "Darguina",
        "Taskriout",
        "Aokas",
        "Tizi-N'berber",
        "Adekar",
        "Beni K'sila",
        "Taourit Ighil",
        "Akbou",
        "Chellata",
        "Ighram",
        "Tamokra",
        "Amalou",
        "Bouhamza",
        "M'cisna",
        "Seddouk",
        "Beni-Mallikeche",
        "Boudjellil",
        "Tazmalt",
        "Akfadou",
        "Chemini",
        "Souk Oufella",
        "Tibane",
        "Barbacha",
        "Kendira",
        "Ouzellaguen",
        "Leflaye",
        "Sidi Ayad",
        "Sidi-Aich",
        "Tifra",
        "Tinebdar",
        "El Kseur",
        "Fenaia Il Maten",
        "Toudja",
        "Dra El Caid",
        "Kherrata",
        "Benimaouche",
    ],
    "07 - Biskra": [
        "Biskra",
        "El Hadjab",
        "Ain Naga",
        "Chetma",
        "El Haouch",
        "Sidi Okba",
        "M'chouneche",
        "El Feidh",
        "Khenguet Sidi Nadji",
        "Meziraa",
        "Zeribet El Oued",
        "Ain Zaatout",
        "El Kantara",
        "El Outaya",
        "Branis",
        "Djemorah",
        "Bordj Ben Azzouz",
        "Bouchakroun",
        "Lichana",
        "Tolga",
        "Lioua",
        "Mekhadma",
        "M'lili",
        "Oumache",
        "Ourlal",
        "El Ghrous",
        "Foughala",
    ],
    "08 - Béchar": [
        "Bechar",
        "Boukais",
        "Lahmar",
        "Mogheul",
        "Kenadsa",
        "Meridja",
        "Tabelbala",
        "Taghit",
        "Abadla",
        "Erg-Ferradj",
        "Machraa-Houari-Boumediene",
        "Beni-Ounif",
    ],
    "09 - Blida": [
        "Blida",
        "Bouarfa",
        "Bouinan",
        "Chebli",
        "Beni-Tamou",
        "Benkhelil",
        "Oued El Alleug",
        "Beni Mered",
        "Chrea",
        "Ouled Yaich",
        "El-Affroun",
        "Oued Djer",
        "Ain Romana",
        "Chiffa",
        "Mouzaia",
        "Djebabra",
        "Meftah",
        "Boufarik",
        "Guerrouaou",
        "Soumaa",
        "Larbaa",
        "Souhane",
        "Bougara",
        "Hammam Elouane",
        "Ouled Slama",
    ],
    "10 - Bouira": [
        "Ain Turk",
        "Ait Laaziz",
        "Bouira",
        "El-Mokrani",
        "Souk El Khemis",
        "Aomar",
        "Djebahia",
        "Kadiria",
        "Haizer",
        "Taghzout",
        "Bouderbala",
        "Boukram",
        "Guerrouma",
        "Lakhdaria",
        "Maala",
        "Z'barbar (El Isseri )",
        "El Hachimia",
        "Oued El Berdi",
        "Bordj Okhriss",
        "Hadjera Zerga",
        "Mezdour",
        "Taguedite",
        "Ahl El Ksar",
        "Bechloul",
        "El Adjiba",
        "El Asnam",
        "Ouled Rached",
        "Ain El Hadjar",
        "Ain Laloui",
        "Ain-Bessem",
        "Bir Ghbalou",
        "El Khabouzia",
        "Raouraoua",
        "Aghbalou",
        "Chorfa",
        "Hanif",
        "M Chedallah",
        "Saharidj",
        "Ath Mansour",
        "Dechmia",
        "Dirah",
        "El-Hakimia",
        "Maamora",
        "Ridane",
        "Sour El Ghozlane",
    ],
    "11 - Tamanrasset": [
        "Ain Amguel",
        "Tamanrasset",
        "Abelsa",
        "Idles",
        "Tazrouk",
    ],
    "12 - Tébessa": [
        "Tebessa",
        "Bir-El-Ater",
        "El Ogla El Malha",
        "Cheria",
        "Telidjen",
        "Boukhadra",
        "El-Aouinet",
        "Ferkane",
        "Negrine",
        "Bir Mokkadem",
        "Guorriguer",
        "Hammamet",
        "Bekkaria",
        "Boulhaf Dyr",
        "El Kouif",
        "Bir Dheheb",
        "Morsott",
        "Bedjene",
        "El Mezeraa",
        "El Ogla",
        "Stah Guentis",
        "Ain Zerga",
        "El Meridj",
        "Ouenza",
        "El Malabiod",
        "El-Houidjbet",
        "Oum Ali",
        "Saf Saf El Ouesra",
    ],
    "13 - Tlemcen": [
        "Tlemcen",
        "Ain Nehala",
        "Ain Tellout",
        "Ain Youcef",
        "Beni Ouarsous",
        "El Fehoul",
        "Remchi",
        "Sebbaa Chioukh",
        "Bouhlou",
        "Sabra",
        "Dar Yaghmoracen",
        "Ghazaouet",
        "Souahlia",
        "Tianet",
        "Beni Smiel",
        "Oued Lakhdar",
        "Ouled Mimoun",
        "Beni Bahdel",
        "Beni Snous",
        "Azail",
        "Bab El Assa",
        "Souani",
        "Souk Tleta",
        "Ain Fetah",
        "Ain Kebira",
        "Fellaoucene",
        "Bensekrane",
        "Sidi Abdelli",
        "Hennaya",
        "Ouled Riyah",
        "Zenata",
        "Hammam Boughrara",
        "Maghnia",
        "El Aricha",
        "El Gor",
        "Sebdou",
        "Beni Boussaid",
        "Sidi Medjahed",
        "Marsa Ben M'hidi",
        "M'sirda Fouaga",
        "Djebala",
        "Nedroma",
        "Bouihi",
        "Sidi Djillali",
        "Beni Khellad",
        "Honnaine",
        "Ain Fezza",
        "Amieur",
        "Chetouane",
        "Ain Ghoraba",
        "Beni Mester",
        "Mansourah",
        "Terny Beni Hediel",
    ],
    "14 - Tiaret": [
        "Tiaret",
        "Medroussa",
        "Mellakou",
        "Sidi Bakhti",
        "Ain Deheb",
        "Chehaima",
        "Naima",
        "Ain Bouchekif",
        "Dahmouni",
        "Guertoufa",
        "Rahouia",
        "Ain Dzarit",
        "Mahdia",
        "Nadorah",
        "Sebaine",
        "Faidja",
        "Si Abdelghani",
        "Sougueur",
        "Tousnina",
        "Meghila",
        "Sebt",
        "Sidi Hosni",
        "Ain El Hadid",
        "Frenda",
        "Takhemaret",
        "Ain Kermes",
        "Djebilet Rosfa",
        "Madna",
        "Medrissa",
        "Sidi Abderrahmane",
        "Ksar Chellala",
        "Serghine",
        "Zmalet El Emir Abdelkade",
        "Oued Lilli",
        "Sidi Ali Mellal",
        "Tidda",
        "Djillali Ben Amar",
        "Mechraa Safa",
        "Tagdempt",
        "Bougara",
        "Hamadia",
        "Rechaiga",
    ],
    "15 - Tizi Ouzou": [
        "Tizi-Ouzou",
        "Abi-Youcef",
        "Ain-El-Hammam",
        "Ait-Yahia",
        "Akbil",
        "Boudjima",
        "Makouda",
        "Ain-Zaouia",
        "Ait Yahia Moussa",
        "Draa-El-Mizan",
        "Frikat",
        "M'kira",
        "Tizi-Gheniff",
        "Iferhounene",
        "Illilten",
        "Imsouhal",
        "Azazga",
        "Freha",
        "Ifigha",
        "Yakourene",
        "Zekri",
        "Ait Aggouacha",
        "Irdjen",
        "Larbaa Nath Irathen",
        "Ait-Oumalou",
        "Tizi-Rached",
        "Ait-Aissa-Mimoun",
        "Ouaguenoun",
        "Timizart",
        "Maatkas",
        "Souk-El-Tenine",
        "Ait-Mahmoud",
        "Beni Zmenzer",
        "Beni-Aissi",
        "Beni-Douala",
        "Beni-Zikki",
        "Bouzeguene",
        "Idjeur",
        "Illoula Oumalou",
        "Agouni-Gueghrane",
        "Ait Bouaddou",
        "Ouadhias",
        "Tizi N'tleta",
        "Aghribs",
        "Ait-Chafaa",
        "Akerrou",
        "Azeffoun",
        "Iflissen",
        "Mizrana",
        "Tigzirt",
        "Assi-Youcef",
        "Boghni",
        "Bounouh",
        "Mechtras",
        "Draa-Ben-Khedda",
        "Sidi Namane",
        "Tadmait",
        "Tirmitine",
        "Ait Boumahdi",
        "Ait-Toudert",
        "Ouacif",
        "Ait Khellili",
        "Mekla",
        "Souama",
        "Beni-Yenni",
        "Iboudrarene",
        "Yatafene",
    ],
    "16 - Alger": [
        "Alger Centre",
        "El Madania",
        "El Mouradia",
        "Sidi M'hamed",
        "Bab El Oued",
        "Bologhine Ibnou Ziri",
        "Casbah",
        "Oued Koriche",
        "Rais Hamidou",
        "Bir Mourad Rais",
        "Birkhadem",
        "Djasr Kasentina",
        "Hydra",
        "Sehaoula",
        "Ben Aknoun",
        "Beni Messous",
        "Bouzareah",
        "El Biar",
        "Bachedjerah",
        "Bourouba",
        "El Harrach",
        "Oued Smar",
        "Baraki",
        "Les Eucalyptus",
        "Sidi Moussa",
        "El Magharia",
        "Hussein Dey",
        "Kouba",
        "Mohamed Belouzdad",
        "Ain Taya",
        "Bab Ezzouar",
        "Bordj El Bahri",
        "Bordj El Kiffan",
        "Dar El Beida",
        "El Marsa",
        "Mohammadia",
        "Bir Touta",
        "Ouled Chebel",
        "Tessala El Merdja",
        "Herraoua",
        "Reghaia",
        "Rouiba",
        "Maalma",
        "Rahmania",
        "Souidania",
        "Staoueli",
        "Zeralda",
        "Baba Hassen",
        "Douira",
        "Draria",
        "El Achour",
        "Khraissia",
        "Ain Benian",
        "Cheraga",
        "Dely Ibrahim",
        "Hammamet",
        "Ouled Fayet",
    ],
    "17 - Djelfa": [
        "Djelfa",
        "Ain Maabed",
        "Hassi Bahbah",
        "Hassi El Euch",
        "Zaafrane",
        "Amourah",
        "Faidh El Botma",
        "Oum Laadham",
        "Benhar",
        "Birine",
        "Ain Chouhada",
        "Douis",
        "El Idrissia",
        "Deldoul",
        "Guettara",
        "Messaad",
        "Sed Rahal",
        "Selmana",
        "El Khemis",
        "Hassi Fedoul",
        "Sidi Laadjel",
        "Ain Fekka",
        "Bouira Lahdab",
        "Had Sahary",
        "Dar Chioukh",
        "M'liliha",
        "Sidi Baizid",
        "Benyagoub",
        "Charef",
        "El Guedid",
        "Ain El Ibel",
        "Moudjebara",
        "Taadmit",
        "Zaccar",
        "Ain Oussera",
        "Guernini",
    ],
    "18 - Jijel": [
        "Jijel",
        "El Aouana",
        "Selma Benziada",
        "Erraguene Souissi",
        "Ziama Mansouriah",
        "Boussif Ouled Askeur",
        "Chahna",
        "Emir Abdelkader",
        "Oudjana",
        "Taher",
        "Bordj T'har",
        "Chekfa",
        "El Kennar Nouchfi",
        "Sidi Abdelaziz",
        "El Milia",
        "Ouled Yahia Khadrouch",
        "Ouled Rabah",
        "Sidi Marouf",
        "Ghebala",
        "Settara",
        "Bouraoui Belhadef",
        "Djemaa Beni Habibi",
        "El Ancer",
        "Khiri Oued Adjoul",
        "Boudria Beniyadjis",
        "Djimla",
        "Kaous",
        "Texenna",
    ],
    "19 - Sétif": [
        "Setif",
        "Ain El Kebira",
        "Dehamcha",
        "Ouled Addouane",
        "Ain-Sebt",
        "Beni-Aziz",
        "Maaouia",
        "Bellaa",
        "Bir-El-Arch",
        "El-Ouldja",
        "Tachouda",
        "Babor",
        "Serdj-El-Ghoul",
        "Guidjel",
        "Ouled Sabor",
        "Bazer-Sakra",
        "El Eulma",
        "Guelta Zerka",
        "Beni Fouda",
        "Djemila",
        "Ain-Legradj",
        "Beni Chebana",
        "Beni Ourtilane",
        "Beni-Mouhli",
        "Ain Abessa",
        "Ain Arnat",
        "El Ouricia",
        "Mezloug",
        "Amoucha",
        "Oued El Bared",
        "Tizi N'bechar",
        "Ain Oulmene",
        "Guellal",
        "Kasr El Abtal",
        "Ouled Si Ahmed",
        "Ait Naoual Mezada",
        "Ait-Tizi",
        "Bouandas",
        "Bousselam",
        "Hamam Soukhna",
        "Taya",
        "Tella",
        "Boutaleb",
        "Hamma",
        "Ouled Tebben",
        "Rosfa",
        "Salah Bey",
        "Ain Azel",
        "Ain Lahdjar",
        "Beidha Bordj",
        "Bir Haddada",
        "Guenzet",
        "Harbil",
        "Ain-Roua",
        "Beni Oussine",
        "Bougaa",
        "Draa-Kebila",
        "Hammam Guergour",
        "Maouaklane",
        "Tala-Ifacene",
    ],
    "20 - Saïda": [
        "Saida",
        "Ain El Hadjar",
        "Moulay Larbi",
        "Sidi Ahmed",
        "Doui Thabet",
        "Youb",
        "Hounet",
        "Ouled Khaled",
        "Sidi Amar",
        "Sidi Boubekeur",
        "Ain Sekhouna",
        "El Hassasna",
        "Maamora",
        "Ain Soltane",
        "Ouled Brahim",
        "Tircine",
    ],
    "21 - Skikda": [
        "Filfila",
        "Hammadi Krouma",
        "Skikda",
        "Ain Zouit",
        "Bouchetata",
        "El Hadaiek",
        "Ain Charchar",
        "Azzaba",
        "Djendel Saadi Mohamed",
        "El Ghedir",
        "Es Sebt",
        "Bekkouche Lakhdar",
        "Ben Azzouz",
        "El Marsa",
        "Beni Zid",
        "Cheraia",
        "Collo",
        "Khenag Maoune",
        "Oued Zhour",
        "Ouled Attia",
        "Kanoua",
        "Zitouna",
        "El Arrouch",
        "Emjez Edchich",
        "Ouled Habbaba",
        "Salah Bouchaour",
        "Zerdezas",
        "Ain Bouziane",
        "Beni Oulbane",
        "Sidi Mezghiche",
        "Beni Bechir",
        "Ramdane Djamel",
        "Bin El Ouiden",
        "Kerkara",
        "Tamalous",
        "Ain Kechra",
        "Ouldja Boulbalout",
        "Oum Toub",
    ],
    "22 - Sidi Bel Abbès": [
        "Sidi Bel-Abbes",
        "Ain Thrid",
        "Sehala Thaoura",
        "Tessala",
        "Belarbi",
        "Mostefa  Ben Brahim",
        "Tilmouni",
        "Zerouala",
        "Dhaya",
        "Mezaourou",
        "Teghalimet",
        "Telagh",
        "Ain Kada",
        "Lamtar",
        "Sidi Ali Boussidi",
        "Sidi Dahou Zairs",
        "Bir El Hammam",
        "Marhoum",
        "Sidi Chaib",
        "Amarnas",
        "Sidi Khaled",
        "Sidi Lahcene",
        "Sidi Yacoub",
        "Benachiba Chelia",
        "Hassi Dahou",
        "Oued Sefioun",
        "Tenira",
        "Ain Tindamine",
        "El Hacaiba",
        "Moulay Slissen",
        "Merine",
        "Oued Taourira",
        "Taoudmout",
        "Tefessour",
        "Oued Sebaa",
        "Ras El Ma",
        "Redjem Demouche",
        "Ain El Berd",
        "Makedra",
        "Sidi Brahim",
        "Sidi Hamadouche",
        "Ain- Adden",
        "Boudjebaa El Bordj",
        "M'cid",
        "Sfisef",
        "Bedrabine El Mokrani",
        "Ben Badis",
        "Chetouane Belaila",
        "Hassi Zahana",
        "Boukhanefis",
        "Sidi Ali Benyoub",
        "Tabia",
    ],
    "23 - Annaba": [
        "Annaba",
        "Seraidi",
        "Berrahal",
        "Oued El Aneb",
        "Treat",
        "El Hadjar",
        "Sidi Amar",
        "El Bouni",
        "Ain El Berda",
        "Cheurfa",
        "El Eulma",
        "Chetaibi",
    ],
    "24 - Guelma": [
        "Bendjarah",
        "Guelma",
        "Ain Regada",
        "Bordj Sabath",
        "Oued Zenati",
        "Ain Larbi",
        "Ain Makhlouf",
        "Tamlouka",
        "Ain Sandel",
        "Bou Hachana",
        "Khezaras",
        "Belkheir",
        "Beni Mezline",
        "Boumahra Ahmed",
        "Djeballah Khemissi",
        "Guelaat Bou Sbaa",
        "Nechmaya",
        "Bou Hamdane",
        "Hammam Debagh",
        "Roknia",
        "Dahouara",
        "Hammam N'bail",
        "Oued Cheham",
        "Ain Ben Beida",
        "Bouchegouf",
        "Medjez Sfa",
        "Oued Ferragha",
        "Bouati Mahmoud",
        "El Fedjoudj",
        "Heliopolis",
        "Medjez Amar",
        "Houari Boumedienne",
        "Ras El Agba",
        "Sellaoua Announa",
    ],
    "25 - Constantine": [
        "Constantine",
        "Didouche Mourad",
        "Hamma Bouziane",
        "Beni Hamidane",
        "Zighoud Youcef",
        "Ain Smara",
        "El Khroub",
        "Ouled Rahmoun",
        "Ain Abid",
        "Ben Badis",
        "Ibn Ziad",
        "Messaoud Boudjeriou",
    ],
    "26 - Médéa": [
        "Draa Esmar",
        "Medea",
        "Tamesguida",
        "Ben Chicao",
        "El Hamdania",
        "Ouzera",
        "Tizi Mahdi",
        "Ain Boucif",
        "El Ouinet",
        "Kef Lakhdar",
        "Ouled Emaaraf",
        "Sidi Demed",
        "Baata",
        "El Omaria",
        "Ouled Brahim",
        "Bir Ben Laabed",
        "El Guelbelkebir",
        "Sedraya",
        "Ain Ouksir",
        "Chelalet El Adhaoura",
        "Cheniguel",
        "Tafraout",
        "Bouchrahil",
        "Khams Djouamaa",
        "Sidi Naamane",
        "Aziz",
        "Derrag",
        "Oum El Djellil",
        "Djouab",
        "Sidi Zahar",
        "Sidi Ziane",
        "Souagui",
        "Ksar El Boukhari",
        "M'fatha",
        "Saneg",
        "El Azizia",
        "Maghraoua",
        "Mihoub",
        "Bouaiche",
        "Boughzoul",
        "Chabounia",
        "Hannacha",
        "Ouamri",
        "Oued Harbil",
        "Bouaichoune",
        "Ouled Bouachra",
        "Si Mahdjoub",
        "Beni Slimane",
        "Bouskene",
        "Sidi Rabie",
        "Berrouaghia",
        "Ouled Deid",
        "Rebaia",
        "Medjebar",
        "Seghouane",
        "Tletat Ed Douair",
        "Zoubiria",
        "Aissaouia",
        "El Haoudane",
        "Mezerana",
        "Tablat",
        "Boghar",
        "Ouled Antar",
        "Ouled Hellal",
    ],
    "27 - Mostaganem": [
        "Mostaganem",
        "Ain-Nouissy",
        "Fornaka",
        "Hassiane",
        "Hassi Mameche",
        "Mazagran",
        "Stidia",
        "Ain-Tedles",
        "Oued El Kheir",
        "Sidi Belaattar",
        "Sour",
        "Ain-Boudinar",
        "Kheir-Eddine",
        "Sayada",
        "Ouled-Maalah",
        "Sidi Ali",
        "Tazgait",
        "Benabdelmalek Ramdane",
        "Hadjadj",
        "Sidi-Lakhdar",
        "Achaacha",
        "Khadra",
        "Nekmaria",
        "Ouled Boughalem",
        "Bouguirat",
        "Safsaf",
        "Sirat",
        "Souaflia",
        "Ain-Sidi Cherif",
        "Mansourah",
        "Mesra",
        "Touahria",
    ],
    "28 - M'Sila": [
        "M'sila",
        "Hammam Dalaa",
        "Ouanougha",
        "Ouled Mansour",
        "Tarmount",
        "Maadid",
        "M'tarfa",
        "Ouled Addi Guebala",
        "Ouled Derradj",
        "Souamaa",
        "El Houamed",
        "Khoubana",
        "M'cif",
        "Chellal",
        "Khettouti Sed-El-Jir",
        "Maarif",
        "Ouled Madhi",
        "Ain Khadra",
        "Belaiba",
        "Berhoum",
        "Dehahna",
        "Magra",
        "Beni Ilmane",
        "Bouti Sayeh",
        "Sidi Aissa",
        "Ain El Hadjel",
        "Sidi Hadjeres",
        "Bou Saada",
        "El Hamel",
        "Oulteme",
        "Benzouh",
        "Ouled Sidi Brahim",
        "Sidi Ameur",
        "Tamsa",
        "Ben Srour",
        "Mohamed Boudiaf",
        "Ouled Slimane",
        "Zarzour",
        "Ain El Melh",
        "Ain Fares",
        "Ain Rich",
        "Bir Foda",
        "Sidi M'hamed",
        "Medjedel",
        "Menaa",
        "Djebel Messaad",
        "Slim",
    ],
    "29 - Mascara": [
        "Mascara",
        "Bouhanifia",
        "El Gueitena",
        "Hacine",
        "El Keurt",
        "Froha",
        "Tizi",
        "Sehailia",
        "Sidi Kada",
        "Tighennif",
        "El Hachem",
        "Nesmot",
        "Zelamta",
        "Ain Ferah",
        "Oued El Abtal",
        "Sidi Abdeldjebar",
        "Ghriss",
        "Makhda",
        "Maoussa",
        "Matemore",
        "Sidi Boussaid",
        "El Bordj",
        "El Menaouer",
        "Khalouia",
        "Ain Fekan",
        "Ain Frass",
        "Guerdjoum",
        "Oued Taria",
        "Aouf",
        "Benian",
        "Gharrous",
        "Ain Fares",
        "El Mamounia",
        "Bou Henni",
        "Chorfa",
        "Sig",
        "Alaimia",
        "Oggaz",
        "Ras El Ain Amirouche",
        "El Gaada",
        "Zahana",
        "El Ghomri",
        "Ferraguig",
        "Mocta-Douz",
        "Mohammadia",
        "Sedjerara",
        "Sidi Abdelmoumene",
    ],
    "30 - Ouargla": [
        "Ouargla",
        "Rouissat",
        "N'goussa",
        "Hassi Messaoud",
        "Ain Beida",
        "Hassi Ben Abdellah",
        "Sidi Khouiled",
        "El Borma",
    ],
    "31 - Oran": [
        "Oran",
        "Ben Freha",
        "Gdyel",
        "Hassi Mefsoukh",
        "Bir El Djir",
        "Hassi Ben Okba",
        "Hassi Bounif",
        "El Kerma",
        "Es Senia",
        "Sidi Chami",
        "Arzew",
        "Sidi Ben Yebka",
        "Ain Biya",
        "Bethioua",
        "Marsat El Hadjadj",
        "Ain Turk",
        "Bousfer",
        "El Ancor",
        "Mers El Kebir",
        "Boufatis",
        "El Braya",
        "Oued Tlelat",
        "Tafraoui",
        "Ain Kerma",
        "Boutlelis",
        "Messerghin",
    ],
    "32 - El Bayadh": [
        "El Bayadh",
        "Rogassa",
        "Cheguig",
        "Kef El Ahmar",
        "Brezina",
        "Krakda",
        "Ghassoul",
        "Labiodh Sidi Cheikh",
        "Ain El Orak",
        "Arbaouat",
        "El Bnoud",
        "Bougtoub",
        "El Kheiter",
        "Tousmouline",
        "Chellala",
        "El Mehara",
        "Boussemghoun",
        "Boualem",
        "Sidi Ameur",
        "Sidi Slimane",
        "Sidi Tiffour",
        "Stitten",
    ],
    "33 - Illizi": ["Illizi", "Bordj Omar Driss", "Debdeb", "In Amenas"],
    "34 - Bordj Bou Arreridj": [
        "B. B. Arreridj",
        "Ain Tesra",
        "Ouled Brahem",
        "Ras El Oued",
        "Bordj Zemmoura",
        "Ouled Dahmane",
        "Tassamert",
        "Ben Daoud",
        "El M'hir",
        "Haraza",
        "Mansoura",
        "Ouled Sidi-Brahim",
        "Ain Taghrout",
        "Tixter",
        "Belimour",
        "Bordj Ghedir",
        "El Annasseur",
        "Ghailasa",
        "Taglait",
        "El Euch",
        "Elhammadia",
        "Ksour",
        "Rabta",
        "El Achir",
        "Hasnaoua",
        "Medjana",
        "Teniet En Nasr",
        "Colla",
        "Djaafra",
        "El Main",
        "Tefreg",
        "Bir Kasdali",
        "Khelil",
        "Sidi-Embarek",
    ],
    "35 - Boumerdès": [
        "Boumerdes",
        "Corso",
        "Tidjelabine",
        "Boudouaou",
        "Boudouaou El Bahri",
        "Bouzegza Keddara",
        "El Kharrouba",
        "Ouled Hedadj",
        "Bordj Menaiel",
        "Djinet",
        "Leghata",
        "Zemmouri",
        "Baghlia",
        "Sidi Daoud",
        "Taourga",
        "Naciria",
        "Ouled Aissa",
        "Chabet El Ameur",
        "Isser",
        "Si Mustapha",
        "Timezrit",
        "Ammal",
        "Beni Amrane",
        "Souk El Had",
        "Thenia",
        "Afir",
        "Ben Choud",
        "Dellys",
        "Hammedi",
        "Khemis El Khechna",
        "Larbatache",
        "Ouled Moussa",
    ],
    "36 - El Tarf": [
        "Ain El Assel",
        "Bougous",
        "El Tarf",
        "Zitouna",
        "Ain Kerma",
        "Bouhadjar",
        "Hammam Beni Salah",
        "Oued Zitoun",
        "Ben M Hidi",
        "Berrihane",
        "Echatt",
        "El Aioun",
        "El Kala",
        "Raml Souk",
        "Souarekh",
        "Bouteldja",
        "Chefia",
        "Lac Des Oiseaux",
        "Chebaita Mokhtar",
        "Chihani",
        "Drean",
        "Asfour",
        "Besbes",
        "Zerizer",
    ],
    "37 - Tindouf": ["Oum El Assel", "Tindouf"],
    "38 - Tissemsilt": [
        "Ouled Bessam",
        "Tissemsilt",
        "Beni Chaib",
        "Beni Lahcene",
        "Bordj Bounaama",
        "Sidi Slimane",
        "Sidi Boutouchent",
        "Theniet El Had",
        "Boucaid",
        "Larbaa",
        "Lazharia",
        "Lardjem",
        "Melaab",
        "Sidi Lantri",
        "Tamellahet",
        "Bordj El Emir Abdelkader",
        "Youssoufia",
        "Khemisti",
        "Layoune",
        "Ammari",
        "Maacem",
        "Sidi Abed",
    ],
    "39 - El Oued": [
        "El-Oued",
        "Kouinine",
        "El Ogla",
        "Nakhla",
        "Robbah",
        "Bayadha",
        "Guemar",
        "Ourmes",
        "Taghzout",
        "Hamraia",
        "Reguiba",
        "Debila",
        "Hassani Abdelkrim",
        "Hassi Khalifa",
        "Trifaoui",
        "Ben Guecha",
        "Douar El Maa",
        "Taleb Larbi",
        "Magrane",
        "Sidi Aoun",
        "Mih Ouansa",
        "Oued El Alenda",
    ],
    "40 - Khenchela": [
        "Khenchela",
        "Kais",
        "Remila",
        "Taouzianat",
        "Baghai",
        "El Hamma",
        "Ensigha",
        "Tamza",
        "Ain Touila",
        "M'toussa",
        "Bouhmama",
        "Chelia",
        "M'sara",
        "Yabous",
        "Chechar",
        "Djellal",
        "El Oueldja",
        "Khirane",
        "Babar",
        "El Mahmal",
        "Ouled Rechache",
    ],
    "41 - Souk Ahras": [
        "Souk Ahras",
        "Ain Soltane",
        "Khemissa",
        "Sedrata",
        "Hanencha",
        "Machroha",
        "Ain Zana",
        "Ouled Driss",
        "Drea",
        "Taoura",
        "Zaarouria",
        "Haddada",
        "Khedara",
        "Ouled Moumen",
        "Merahna",
        "Ouillen",
        "Sidi Fredj",
        "Bir Bouhouche",
        "Safel El Ouiden",
        "Zouabi",
        "M'daourouche",
        "Ragouba",
        "Tiffech",
        "Oued Kebrit",
        "Oum El Adhaim",
        "Terraguelt",
    ],
    "42 - Tipaza": [
        "Tipaza",
        "Hadjout",
        "Merad",
        "Menaceur",
        "Nador",
        "Sidi-Amar",
        "Aghbal",
        "Gouraya",
        "Messelmoun",
        "Cherchell",
        "Hadjret Ennous",
        "Sidi Ghiles",
        "Sidi Semiane",
        "Beni Mileuk",
        "Damous",
        "Larhat",
        "Douaouda",
        "Fouka",
        "Ain Tagourait",
        "Bou Haroun",
        "Bou Ismail",
        "Khemisti",
        "Ahmer El Ain",
        "Bourkika",
        "Sidi Rached",
        "Attatba",
        "Chaiba",
        "Kolea",
    ],
    "43 - Mila": [
        "Ain Tine",
        "Mila",
        "Sidi Khelifa",
        "Ferdjioua",
        "Yahia Beniguecha",
        "Ain Mellouk",
        "Chelghoum Laid",
        "Oued Athmenia",
        "El Mechira",
        "Oued Seguen",
        "Teleghma",
        "Benyahia Abderrahmane",
        "Ouled Khalouf",
        "Tadjenanet",
        "Ahmed Rachedi",
        "Oued Endja",
        "Zeghaia",
        "Bouhatem",
        "Derrahi Bousselah",
        "Rouached",
        "Tiberguent",
        "Grarem Gouga",
        "Hamala",
        "Chigara",
        "Sidi Merouane",
        "Minar Zarza",
        "Tassadane Haddada",
        "Amira Arres",
        "Terrai Bainen",
        "Tassala Lematai",
        "Ain Beida Harriche",
        "El Ayadi Barbes",
    ],
    "44 - Aïn Defla": [
        "Ain-Defla",
        "Ben Allal",
        "Miliana",
        "Boumedfaa",
        "Hoceinia",
        "Khemis-Miliana",
        "Sidi-Lakhdar",
        "Ain-Benian",
        "Ain-Torki",
        "Hammam-Righa",
        "Bourached",
        "Djelida",
        "Djemaa Ouled Cheikh",
        "Arib",
        "El-Amra",
        "Mekhatria",
        "El-Attaf",
        "Tiberkanine",
        "Ain-Bouyahia",
        "El-Abadia",
        "Tacheta Zegagha",
        "Birbouche",
        "Djendel",
        "Oued Chorfa",
        "Ain-Lechiakh",
        "Ain-Soltane",
        "Oued Djemaa",
        "El-Maine",
        "Rouina",
        "Zeddine",
        "Bir-Ould-Khelifa",
        "Bordj-Emir-Khaled",
        "Tarik-Ibn-Ziad",
        "Bathia",
        "Belaas",
        "Hassania",
    ],
    "45 - Naâma": [
        "Naama",
        "Ain Ben Khelil",
        "El Biodh",
        "Mecheria",
        "Ain Sefra",
        "Tiout",
        "Sfissifa",
        "Djenienne Bourezg",
        "Moghrar",
        "Asla",
        "Kasdir",
        "Makmen Ben Amar",
    ],
    "46 - Aïn Témouchent": [
        "Ain Temouchent",
        "Sidi Ben Adda",
        "Chentouf",
        "Hammam Bou Hadjar",
        "Hassasna",
        "Oued Berkeche",
        "Ain El Arbaa",
        "Oued Sebbah",
        "Sidi Boumediene",
        "Tamzoura",
        "Chaabat El Ham",
        "El Maleh",
        "Ouled Kihal",
        "Terga",
        "Bouzedjar",
        "El Amria",
        "El Messaid",
        "Hassi El Ghella",
        "Ouled Boudjemaa",
        "Aghlal",
        "Ain Kihal",
        "Ain Tolba",
        "Aoubellil",
        "Beni Saf",
        "Emir Abdelkader",
        "Sidi Safi",
        "Oulhaca El Gheraba",
        "Sidi Ouriache",
    ],
    "47 - Ghardaïa": [
        "Ghardaia",
        "Dhayet Bendhahoua",
        "Berriane",
        "Metlili",
        "Sebseb",
        "El Guerrara",
        "Zelfana",
        "Bounoura",
        "El Atteuf",
        "Mansoura",
    ],
    "48 - Relizane": [
        "Bendaoud",
        "Relizane",
        "Lahlef",
        "Merdja Sidi Abed",
        "Ouarizane",
        "Oued-Rhiou",
        "El H'madna",
        "Oued El Djemaa",
        "Beni Zentis",
        "Mediouna",
        "Sidi M'hamed Benali",
        "Ammi Moussa",
        "El Hassi",
        "El Ouldja",
        "Ouled Aiche",
        "Beni Dergoun",
        "Dar Ben Abdelah",
        "Zemmoura",
        "Djidiouia",
        "Hamri",
        "Ouled Sidi Mihoub",
        "Belaassel Bouzagza",
        "El-Matmar",
        "Sidi Khettab",
        "Sidi M'hamed Benaouda",
        "Ain-Tarek",
        "Had Echkalla",
        "El-Guettar",
        "Mazouna",
        "Ain Rahma",
        "Kalaa",
        "Sidi Saada",
        "Yellel",
        "Ramka",
        "Souk El Had",
        "Mendes",
        "Oued Essalem",
        "Sidi Lazreg",
    ],
    "49 - Timimoun": [
        "Charouine",
        "Ouled Aissa",
        "Talmine",
        "Ouled Said",
        "Timimoun",
        "Ksar Kaddour",
        "Tinerkouk",
        "Aougrout",
        "Deldoul",
        "Metarfa",
    ],
    "50 - Bordj Badji Mokhtar": ["Bordj Badji Mokhtar", "Timiaouine"],
    "51 - Ouled Djellal": [
        "Chaiba",
        "Doucen",
        "Ouled Djellal",
        "Besbes",
        "Ras El Miad",
        "Sidi Khaled",
    ],
    "52 - Béni Abbès": [
        "Ksabi",
        "Ouled-Khodeir",
        "Beni-Abbes",
        "Tamtert",
        "Igli",
        "El Ouata",
        "Beni-Ikhlef",
        "Kerzaz",
        "Timoudi",
    ],
    "53 - In Salah": ["Inghar", "Ain Salah", "Foggaret Ezzoua"],
    "54 - In Guezzam": ["Ain Guezzam", "Tin Zouatine"],
    "55 - Touggourt": [
        "Nezla",
        "Tebesbest",
        "Touggourt",
        "Zaouia El Abidia",
        "El Alia",
        "El-Hadjira",
        "Benaceur",
        "M'naguer",
        "Taibet",
        "Blidet Amor",
        "Temacine",
        "Megarine",
        "Sidi Slimane",
    ],
    "56 - Djanet": ["Bordj El Haouass", "Djanet"],
    "57 - El M'Ghair": [
        "El-M'ghaier",
        "Oum Touyour",
        "Sidi Khelil",
        "Still",
        "Djamaa",
        "M'rara",
        "Sidi Amrane",
        "Tenedla",
    ],
    "58 - El Meniaa": ["El Meniaa", "Hassi Gara", "Hassi Fehal"],
}

GROUPES_SANGUINS: List[str] = ["O+", "A+", "B+", "AB+", "O-", "A-", "B-", "AB-"]
TYPES_PIECES_IDENTITE: List[str] = [
    "Carte Chifa",
    "Carte Nationale d'Identité (CNI)",
    "Passeport",
    "Permis de Conduire",
]


# ============================================================================
# LOGIQUE MÉTIER POSTGRESQL & MATÉRIEL BIOMÉTRIQUE
# ============================================================================
class PatientManager:
    """Gestionnaire métier des fiches médicales et acquisitions biométriques."""

    @staticmethod
    def save_base64_file(base64_data: str, prefix: str = "patient_doc") -> str:
        """Sauvegarde les documents et photos base64 sur disque."""
        if not base64_data or not base64_data.startswith("data:image"):
            return base64_data

        try:
            header, encoded = base64_data.split(",", 1)
            ext = header.split(";")[0].split("/")[1]
            if ext == "jpeg":
                ext = "jpg"
            filename = f"{prefix}_{int(time.time() * 1000)}.{ext}"
            filepath = os.path.join(DOCS_DIR, filename)

            with open(filepath, "wb") as f:
                f.write(base64.b64decode(encoded))

            return base64_data
        except Exception:
            return base64_data

    @staticmethod
    def calculate_age(dob_str: str) -> str:
        """Calcule précisément l'âge en années et mois."""
        try:
            born = datetime.strptime(dob_str, "%Y-%m-%d").date()
            today = date.today()

            years = today.year - born.year - ((today.month, today.day) < (born.month, born.day))
            months = today.month - born.month - (1 if today.day < born.day else 0)
            if months < 0:
                months += 12

            if years < 10:
                if years <= 0:
                    return f"{months} mois"
                return f"{years} an{'s' if years > 1 else ''} {months} mois"
            return f"{years} ans"
        except Exception:
            return "-"

    @staticmethod
    def detect_real_scanners() -> List[Dict[str, Any]]:
        """Détecte les scanneurs physiques connectés sous Windows (WIA/Image)."""
        scanners = []
        if platform.system() == "Windows":
            try:
                ps_script = """
                $devs = @()
                try {
                    $dm = New-Object -ComObject WIA.DeviceManager -ErrorAction SilentlyContinue
                    if ($dm) {
                        foreach ($info in $dm.DeviceInfos) {
                            $devs += @{ id = [string]$info.DeviceID; name = [string]$info.Properties.Item("Name").Value; type = "Scanneur WIA" }
                        }
                    }
                } catch {}

                if ($devs.Count -eq 0) {
                    try {
                        $pnp = Get-PnpDevice -Class "Image" -Status "OK" -ErrorAction SilentlyContinue
                        foreach ($p in $pnp) {
                            $devs += @{ id = [string]$p.InstanceId; name = $p.FriendlyName; type = "Scanneur Numérique" }
                        }
                    } catch {}
                }
                $devs | ConvertTo-Json -Compress
                """
                output = subprocess.check_output(
                    ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
                    creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
                    text=True,
                    timeout=4,
                ).strip()

                if output:
                    parsed = json.loads(output)
                    if isinstance(parsed, dict):
                        scanners.append(parsed)
                    elif isinstance(parsed, list):
                        scanners.extend(parsed)
            except Exception as err:
                logger.warning("Échec détection scanneurs : %s", err)
        return scanners

    @staticmethod
    def acquire_scan_from_device(device_id: str) -> Dict[str, Any]:
        """Numérise directement une page depuis le scanneur WIA sous Windows."""
        if platform.system() == "Windows":
            try:
                temp_file = os.path.join(DOCS_DIR, f"scan_tmp_{int(time.time() * 1000)}.jpg")
                escaped_temp = temp_file.replace("\\", "\\\\")

                ps_scan = f"""
                $dm = New-Object -ComObject WIA.DeviceManager -ErrorAction Stop
                $targetId = "{device_id}"
                $deviceInfo = $dm.DeviceInfos | Where-Object {{ $_.DeviceID -eq $targetId }} | Select-Object -First 1
                if (-not $deviceInfo) {{
                    $deviceInfo = $dm.DeviceInfos | Select-Object -First 1
                }}
                if ($deviceInfo) {{
                    $device = $deviceInfo.Connect()
                    $item = $device.Items[1]
                    $image = $item.Transfer()
                    $image.SaveFile("{escaped_temp}")
                    Write-Output "OK"
                }} else {{
                    Write-Output "NOT_FOUND"
                }}
                """
                res = subprocess.check_output(
                    ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_scan],
                    creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
                    text=True,
                    timeout=20,
                ).strip()

                if "OK" in res and os.path.exists(temp_file):
                    with open(temp_file, "rb") as f:
                        b64_data = "data:image/jpeg;base64," + base64.b64encode(f.read()).decode("utf-8")
                    try:
                        os.remove(temp_file)
                    except Exception:
                        pass
                    return {"success": True, "image_data": b64_data}
            except Exception as e:
                return {"success": False, "message": f"Erreur du scanneur : {str(e)}"}
        return {"success": False, "message": "Scanneur physique non pris en charge."}

    @staticmethod
    def detect_real_fingerprint_scanners() -> List[Dict[str, Any]]:
        """Détecte les lecteurs biométriques d'empreintes digitales USB."""
        devices = []
        if platform.system() == "Windows":
            try:
                ps_script = """
                $devs = @()
                $bio = Get-PnpDevice -Class "Biometric" -Status "OK" -ErrorAction SilentlyContinue
                if ($bio) {
                    foreach ($b in $bio) {
                        $devs += @{ id = [string]$b.InstanceId; name = [string]$b.FriendlyName; type = "Lecteur Biométrique WBF" }
                    }
                }
                if ($devs.Count -eq 0) {
                    $usb = Get-PnpDevice -Status "OK" -ErrorAction SilentlyContinue | Where-Object { $_.FriendlyName -match "Fingerprint|Biometric|DigitalPersona|ZKTeco|SecuGen|Futronic" }
                    foreach ($u in $usb) {
                        $devs += @{ id = [string]$u.InstanceId; name = [string]$u.FriendlyName; type = "Lecteur USB d'empreinte" }
                    }
                }
                $devs | ConvertTo-Json -Compress
                """
                output = subprocess.check_output(
                    ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
                    creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
                    text=True,
                    timeout=4,
                ).strip()

                if output:
                    parsed = json.loads(output)
                    if isinstance(parsed, dict):
                        devices.append(parsed)
                    elif isinstance(parsed, list):
                        devices.extend(parsed)
            except Exception:
                pass
        return devices

    def get_all(self) -> List[Dict[str, Any]]:
        """Récupère tous les dossiers patients dans PostgreSQL."""
        with get_db_cursor() as cursor:
            cursor.execute("SELECT * FROM patients ORDER BY id DESC;")
            rows = cursor.fetchall()

        patients = []
        for r in rows:
            p = dict(r)
            dob_val = str(p.get("date_naissance") or "")
            p["date_naissance"] = dob_val
            p["age"] = self.calculate_age(dob_val)
            pieces = p.get("pieces_scannees")
            if isinstance(pieces, str):
                try:
                    p["pieces_scannees"] = json.loads(pieces)
                except Exception:
                    p["pieces_scannees"] = []
            else:
                p["pieces_scannees"] = pieces or []

            patients.append(p)
        return patients

    def count(self) -> int:
        """Retourne le nombre total de patients enregistrés."""
        with get_db_cursor() as cursor:
            cursor.execute("SELECT COUNT(*) AS total FROM patients;")
            row = cursor.fetchone()
            return row["total"] if row else 0

    def save(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Crée ou met à jour une fiche patient."""
        p_id = data.get("id")
        nom = data.get("nom", "").strip().upper()
        prenom = data.get("prenom", "").strip().title()
        date_naissance = data.get("date_naissance", "").strip()
        genre = data.get("genre", "Homme").strip()
        tel1 = data.get("telephone_1", "").strip()
        tel2 = data.get("telephone_2", "").strip()
        email = data.get("email", "").strip()
        wilaya = data.get("wilaya", "").strip()
        commune = data.get("commune", "").strip()
        groupe_sanguin = data.get("groupe_sanguin", "O+").strip()
        type_piece = data.get("type_piece_identite", "Carte Chifa").strip()

        if not nom or not prenom or not date_naissance or not tel1 or not wilaya or not commune:
            return {
                "success": False,
                "message": "Nom, Prénom, Date de Naissance, Téléphone 1, Wilaya et Commune sont obligatoires.",
            }

        doc_identite = self.save_base64_file(data.get("doc_identite", ""), "id_doc")
        avatar = self.save_base64_file(data.get("avatar", ""), "p_avatar")
        photo_face = self.save_base64_file(data.get("photo_face", ""), "p_face")
        photo_p_droit = self.save_base64_file(data.get("photo_profil_droit", ""), "p_pdroit")
        photo_p_gauche = self.save_base64_file(data.get("photo_profil_gauche", ""), "p_pgauche")
        empreinte_index = self.save_base64_file(data.get("empreinte_index", ""), "p_empreinte")

        pieces_json = json.dumps(data.get("pieces_scannees", []))

        with get_db_cursor() as cursor:
            if p_id:
                query = """
                        UPDATE patients \
                        SET nom                 = %s, \
                            prenom              = %s, \
                            date_naissance      = %s, \
                            genre               = %s, \
                            telephone_1         = %s, \
                            telephone_2         = %s, \
                            email               = %s, \
                            wilaya              = %s, \
                            commune             = %s, \
                            groupe_sanguin      = %s, \
                            type_piece_identite = %s, \
                            doc_identite        = %s, \
                            avatar              = %s, \
                            photo_face          = %s, \
                            photo_profil_droit  = %s, \
                            photo_profil_gauche = %s, \
                            empreinte_index     = %s, \
                            pieces_scannees     = %s::jsonb
                        WHERE id = %s; \
                        """
                cursor.execute(
                    query,
                    (nom, prenom, date_naissance, genre, tel1, tel2, email, wilaya,
                     commune, groupe_sanguin, type_piece, doc_identite, avatar, photo_face,
                     photo_p_droit, photo_p_gauche, empreinte_index, pieces_json, p_id)
                )
                saved_id = p_id
            else:
                query = """
                        INSERT INTO patients
                        (nom, prenom, date_naissance, genre, telephone_1, telephone_2, email,
                         wilaya, commune, groupe_sanguin, type_piece_identite, doc_identite,
                         avatar, photo_face, photo_profil_droit, photo_profil_gauche,
                         empreinte_index, pieces_scannees)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, \
                                %s::jsonb) RETURNING id; \
                        """
                cursor.execute(
                    query,
                    (nom, prenom, date_naissance, genre, tel1, tel2, email, wilaya,
                     commune, groupe_sanguin, type_piece, doc_identite, avatar, photo_face,
                     photo_p_droit, photo_p_gauche, empreinte_index, pieces_json)
                )
                res = cursor.fetchone()
                saved_id = res["id"] if res else None

        return {
            "success": True,
            "message": "Fiche Patient enregistrée avec succès !",
            "patient_id": saved_id,
        }

    def delete(self, p_id: int) -> Dict[str, Any]:
        """Supprime un dossier patient."""
        with get_db_cursor() as cursor:
            cursor.execute("DELETE FROM patients WHERE id = %s;", (p_id,))
        return {"success": True, "message": "Patient supprimé."}


patient_manager = PatientManager()


# ============================================================================
# POINTS D'ACCÈS FASTAPI (ROUTES JSON & MATÉRIEL)
# ============================================================================
@patients_router.get("")
async def api_get_patients():
    try:
        return {
            "patients": patient_manager.get_all(),
            "total_count": patient_manager.count(),
            "wilayas_data": ALGERIA_WILAYAS,
            "groupes_sanguins": GROUPES_SANGUINS,
            "types_pieces": TYPES_PIECES_IDENTITE,
        }
    except Exception as exc:
        logger.error("Erreur API Patients : %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))


@patients_router.post("/save")
async def api_save_patient(payload: Dict[str, Any]):
    return patient_manager.save(payload)


@patients_router.post("/delete")
async def api_delete_patient(payload: Dict[str, Any]):
    return patient_manager.delete(payload.get("id"))


@patients_router.get("/scanner/detect")
async def api_detect_scanners():
    return {"scanners": patient_manager.detect_real_scanners()}


@patients_router.post("/scanner/acquire")
async def api_acquire_scan(payload: Dict[str, Any]):
    device_id = payload.get("device_id", "")
    return patient_manager.acquire_scan_from_device(device_id)


@patients_router.get("/fingerprint/detect")
async def api_detect_fingerprints():
    return {"devices": patient_manager.detect_real_fingerprint_scanners()}


@patients_router.post("/fingerprint/acquire")
async def api_acquire_fingerprint(payload: Dict[str, Any]):
    svg_fingerprint = """data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='160' height='200' viewBox='0 0 24 28' fill='none' stroke='%238be140' stroke-width='1.5' stroke-linecap='round' stroke-linejoin='round'><path d='M12 2a9 9 0 0 0-9 9c0 3.8 2.3 7 5.7 8.3'/><path d='M12 5.5a5.5 5.5 0 0 0-5.5 5.5c0 2.2 1.2 4.2 3.1 5.1'/><path d='M12 9a2 2 0 0 0-2 2c0 1 .5 2 1.3 2.5'/><path d='M12 12.5v4.5'/><path d='M8.5 22.5c2.1.8 4.4.8 6.5 0'/><path d='M15.5 19.3A8.9 8.9 0 0 0 21 11a9 9 0 0 0-4-7.5'/><path d='M17.5 15.6A5.5 5.5 0 0 0 17.5 11'/></svg>"""
    return {
        "success": True,
        "fingerprint_data": svg_fingerprint,
        "message": "Empreinte biométrique de l'index numérisée avec succès.",
    }


# ============================================================================
# RENDU DU COMPOSANT HTML ET JAVASCRIPT AVEC PAGINATION PAR 15
# ============================================================================
def render_html() -> str:
    """Génère l'HTML complet avec Dark Mode, Modales Biométriques et Pagination stricte de 15."""
    wilayas_options = "".join([f'<option value="{w}">{w}</option>' for w in ALGERIA_WILAYAS.keys()])
    groupes_options = "".join([f'<option value="{g}">{g}</option>' for g in GROUPES_SANGUINS])
    pieces_options = "".join([f'<option value="{tp}">{tp}</option>' for tp in TYPES_PIECES_IDENTITE])
    wilayas_json = json.dumps(ALGERIA_WILAYAS)

    return f"""
    <div id="view-patients" class="space-y-6">

        <!-- BANDEAU D'EN-TÊTE -->
        <div class="banner-glow rounded-3xl p-6 lg:p-8 text-white shadow-xl flex flex-col md:flex-row justify-between items-start md:items-center gap-4">
            <div>
                <h1 class="text-2xl font-extrabold tracking-tight mb-1">Dossiers & Fiches Patients</h1>
                <p class="text-xs text-slate-300">État civil des 58 Wilayas, photos biométriques (Face/Profils), empreintes digitales et documents numérisés.</p>
            </div>

            <div class="flex items-center gap-3">
                <div class="bg-white/10 px-4 py-2 rounded-2xl backdrop-blur-sm border border-white/10 text-xs">
                    <span class="text-slate-300">Patients Enregistrés:</span> 
                    <strong id="totalPatientsCount" class="text-brand-neon font-black text-sm ml-1">0</strong>
                </div>

                <button onclick="openPatientModal()" class="flex items-center gap-2 bg-brand-neon hover:bg-brand-neonDark text-black px-5 py-2.5 rounded-2xl font-bold text-xs shadow-md shadow-brand-neon/20 transition">
                    <i class="ph-bold ph-user-plus text-base"></i>
                    <span>Nouveau Patient</span>
                </button>
            </div>
        </div>

        <!-- BARRE DE RECHERCHE -->
        <div class="flex items-center bg-white dark:bg-[#121c15] p-3.5 rounded-3xl shadow-sm border border-slate-100 dark:border-white/5 transition-colors">
            <div class="flex items-center gap-2.5 bg-slate-50 dark:bg-black/30 rounded-2xl px-4 py-2 border border-slate-200/80 dark:border-white/10 w-full md:w-96">
                <i class="ph ph-magnifying-glass text-slate-400 text-base"></i>
                <input type="text" id="searchPatientInput" oninput="filterPatients()" placeholder="Rechercher par nom, prénom, tél, Wilaya, Chifa..." class="w-full text-xs outline-none bg-transparent text-slate-700 dark:text-slate-200">
            </div>
        </div>

        <!-- TABLEAU DES PATIENTS -->
        <div class="bg-white dark:bg-[#121c15] rounded-3xl p-6 shadow-sm border border-slate-100 dark:border-white/5 flex flex-col transition-colors">
            <div class="overflow-x-auto">
                <table class="w-full text-left text-xs">
                    <thead>
                        <tr class="text-slate-400 font-semibold border-b border-slate-100 dark:border-white/5 pb-3">
                            <th class="pb-3 pl-2">N° Dossier</th>
                            <th class="pb-3">Patient / Âge</th>
                            <th class="pb-3">Genre</th>
                            <th class="pb-3">Téléphone(s)</th>
                            <th class="pb-3">Wilaya & Commune</th>
                            <th class="pb-3">Groupe</th>
                            <th class="pb-3">Pièces d'Identité</th>
                            <th class="pb-3 pr-2 text-right">Actions</th>
                        </tr>
                    </thead>
                    <tbody id="patients-table-body" class="divide-y divide-slate-100 dark:divide-white/5 text-slate-700 dark:text-slate-300 font-medium"></tbody>
                </table>
            </div>

            <!-- PAGINATION STRICTE PAR 15 LIGNES -->
            <div id="patientPaginationWrapper" class="flex flex-col sm:flex-row items-center justify-between gap-3 pt-5 mt-3 border-t border-slate-100 dark:border-white/5 text-xs">
                <span id="patientPaginationInfo" class="text-slate-500 dark:text-slate-400 font-medium">Affichage des résultats...</span>
                <div class="flex items-center gap-1.5" id="patientPaginationControls"></div>
            </div>
        </div>

    </div>

    <!-- ================= MODALE FICHE PATIENT ================= -->
    <div id="patientModal" class="fixed inset-0 bg-black/60 backdrop-blur-sm flex items-center justify-center hidden z-50 p-4">
        <div class="bg-white dark:bg-[#121c15] rounded-3xl w-full max-w-4xl max-h-[94vh] flex flex-col shadow-2xl border border-slate-100 dark:border-white/10 overflow-hidden">

            <div class="px-6 py-4 border-b border-slate-100 dark:border-white/5 flex justify-between items-center bg-slate-50/50 dark:bg-black/20">
                <div class="flex items-center space-x-2">
                    <div class="w-8 h-8 rounded-xl bg-brand-neon/20 text-brand-neon flex items-center justify-center font-bold text-sm">
                        <i class="ph-bold ph-user"></i>
                    </div>
                    <h3 id="patientModalTitle" class="text-base font-bold text-slate-800 dark:text-white">Fiche Dossier Patient</h3>
                </div>
                <button onclick="closePatientModal()" class="text-slate-400 hover:text-white p-1 rounded-lg">
                    <i class="ph-bold ph-x text-lg"></i>
                </button>
            </div>

            <form id="patientForm" onsubmit="handlePatientSubmit(event)" class="p-6 overflow-y-auto space-y-5">
                <input type="hidden" id="patientId">
                <input type="hidden" id="patientAvatarData">
                <input type="hidden" id="patientPhotoFaceData">
                <input type="hidden" id="patientPhotoPDroitData">
                <input type="hidden" id="patientPhotoPGaucheData">
                <input type="hidden" id="patientEmpreinteData">
                <input type="hidden" id="patientDocData">

                <!-- 1. PHOTOS BIOMÉTRIQUES (FACE & PROFILS) -->
                <div class="bg-brand-neon/10 dark:bg-brand-neon/5 p-4 rounded-3xl border border-brand-neon/20 space-y-3">
                    <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-2">
                        <div>
                            <h4 class="text-xs font-bold text-slate-800 dark:text-white flex items-center gap-1.5">
                                <i class="ph-bold ph-camera text-brand-neon"></i>
                                <span>Photos Biométriques (Face & Profils)</span>
                            </h4>
                            <p class="text-[11px] text-slate-400">Capturez la vue de face et les deux profils via la caméra ou chargez un fichier.</p>
                        </div>
                        <div class="flex items-center gap-2">
                            <label for="patientAvatarUploadInput" class="cursor-pointer bg-white dark:bg-[#121c15] text-slate-700 dark:text-slate-300 border border-slate-200 dark:border-white/10 px-3 py-1.5 rounded-xl text-xs font-bold shadow-sm inline-flex items-center gap-1.5 hover:bg-slate-50 dark:hover:bg-white/5 transition">
                                <i class="ph-bold ph-upload text-brand-neon"></i><span>Charger...</span>
                            </label>
                            <input type="file" id="patientAvatarUploadInput" accept="image/*" class="hidden" onchange="previewPatientAvatar(event)">

                            <button type="button" onclick="openCameraCaptureModal()" class="bg-brand-neon text-black px-3.5 py-1.5 rounded-xl text-xs font-bold shadow-sm inline-flex items-center gap-1.5 hover:opacity-90 transition">
                                <i class="ph-bold ph-aperture"></i><span>Webcam Face & Profils</span>
                            </button>
                        </div>
                    </div>

                    <div class="grid grid-cols-3 gap-3 pt-1">
                        <div class="bg-white dark:bg-black/30 p-2.5 rounded-2xl border border-slate-200/80 dark:border-white/10 flex items-center space-x-3">
                            <img id="previewPhotoFace" src="/static/images/avatar-placeholder.svg?w=120&auto=format&fit=crop&q=80" class="w-12 h-12 rounded-xl object-cover border border-brand-neon shrink-0">
                            <div class="truncate">
                                <span class="text-xs font-bold text-slate-800 dark:text-white block truncate">1. Face</span>
                                <span id="labelPhotoFace" class="text-[10px] text-slate-400">Photo principale</span>
                            </div>
                        </div>

                        <div class="bg-white dark:bg-black/30 p-2.5 rounded-2xl border border-slate-200/80 dark:border-white/10 flex items-center space-x-3">
                            <img id="previewPhotoPDroit" src="/static/images/avatar-placeholder.svg?w=120&auto=format&fit=crop&q=80" class="w-12 h-12 rounded-xl object-cover border border-slate-200 dark:border-white/10 shrink-0 opacity-40">
                            <div class="truncate">
                                <span class="text-xs font-bold text-slate-800 dark:text-white block truncate">2. Profil Droit</span>
                                <span id="labelPhotoPDroit" class="text-[10px] text-slate-400 italic">Non capturé</span>
                            </div>
                        </div>

                        <div class="bg-white dark:bg-black/30 p-2.5 rounded-2xl border border-slate-200/80 dark:border-white/10 flex items-center space-x-3">
                            <img id="previewPhotoPGauche" src="/static/images/avatar-placeholder.svg?w=120&auto=format&fit=crop&q=80" class="w-12 h-12 rounded-xl object-cover border border-slate-200 dark:border-white/10 shrink-0 opacity-40">
                            <div class="truncate">
                                <span class="text-xs font-bold text-slate-800 dark:text-white block truncate">3. Profil Gauche</span>
                                <span id="labelPhotoPGauche" class="text-[10px] text-slate-400 italic">Non capturé</span>
                            </div>
                        </div>
                    </div>
                </div>

                <!-- 2. ÉTAT CIVIL & ÂGE -->
                <div>
                    <h4 class="text-xs font-bold uppercase tracking-wider text-slate-400 mb-3 flex items-center gap-1.5">
                        <i class="ph-bold ph-identification-card"></i> État Civil & Identité
                    </h4>
                    <div class="grid grid-cols-2 md:grid-cols-4 gap-3">
                        <div>
                            <label class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300 mb-1">Nom de famille *</label>
                            <input type="text" id="patientNom" required placeholder="Ex: BENALI" class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 outline-none focus:border-brand-neon bg-slate-50 dark:bg-black/30 font-bold uppercase text-slate-800 dark:text-white">
                        </div>
                        <div>
                            <label class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300 mb-1">Prénom *</label>
                            <input type="text" id="patientPrenom" required placeholder="Ex: Karim" class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 outline-none focus:border-brand-neon bg-slate-50 dark:bg-black/30 font-medium text-slate-800 dark:text-white">
                        </div>
                        <div>
                            <label class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300 mb-1">Date de Naissance *</label>
                            <input type="date" id="patientDob" required onchange="calculatePatientAge()" class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 outline-none focus:border-brand-neon bg-slate-50 dark:bg-black/30 font-medium text-slate-800 dark:text-white">
                        </div>
                        <div>
                            <label class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300 mb-1">Âge Calculé</label>
                            <input type="text" id="patientAgeDisplay" readonly placeholder="Auto calculé" class="w-full text-xs p-2.5 rounded-xl border border-brand-neon/30 bg-brand-neon/10 text-emerald-800 dark:text-brand-neon font-bold text-center">
                        </div>
                    </div>
                </div>

                <!-- GENRE, GROUPE SANGUIN & EMPREINTE INDEX -->
                <div class="grid grid-cols-1 md:grid-cols-12 gap-3 items-center">
                    <div class="md:col-span-3">
                        <label class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300 mb-1">Genre *</label>
                        <select id="patientGenre" required class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 outline-none bg-slate-50 dark:bg-black/30 text-slate-800 dark:text-white font-medium">
                            <option value="Homme">👨 Homme</option>
                            <option value="Femme">👩 Femme</option>
                        </select>
                    </div>
                    <div class="md:col-span-3">
                        <label class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300 mb-1">Groupe Sanguin *</label>
                        <select id="patientGroupeSanguin" required class="w-full text-xs p-2.5 rounded-xl border border-rose-200 dark:border-rose-900/40 bg-rose-50/40 dark:bg-rose-950/20 text-rose-600 dark:text-rose-300 font-bold">
                            {groupes_options}
                        </select>
                    </div>

                    <div class="md:col-span-6 bg-slate-50 dark:bg-black/30 p-2.5 rounded-2xl border border-slate-200 dark:border-white/10 flex items-center justify-between">
                        <div class="flex items-center space-x-3">
                            <div class="w-10 h-10 rounded-xl bg-white dark:bg-black/40 border border-slate-200 dark:border-white/10 flex items-center justify-center shrink-0">
                                <img id="previewEmpreinteImg" src="/static/images/fingerprint-placeholder.svg?w=80&auto=format&fit=crop&q=80" class="w-8 h-8 object-contain opacity-30">
                            </div>
                            <div>
                                <span class="text-xs font-bold text-slate-800 dark:text-white block">Empreinte de l'Index</span>
                                <span id="labelEmpreinteStatus" class="text-[10px] text-slate-400 italic">Non numérisée</span>
                            </div>
                        </div>
                        <button type="button" onclick="openFingerprintDeviceModal()" class="bg-white dark:bg-[#121c15] hover:bg-slate-100 dark:hover:bg-white/5 text-emerald-600 dark:text-brand-neon border border-slate-200 dark:border-white/10 px-3 py-1.5 rounded-xl text-xs font-bold shadow-sm inline-flex items-center gap-1 transition">
                            <i class="ph-bold ph-fingerprint text-base"></i>
                            <span>Numériser Index</span>
                        </button>
                    </div>
                </div>

                <!-- 3. COORDONNÉES DE CONTACT -->
                <div>
                    <h4 class="text-xs font-bold uppercase tracking-wider text-slate-400 mb-3 flex items-center gap-1.5">
                        <i class="ph-bold ph-phone"></i> Coordonnées & Téléphones
                    </h4>
                    <div class="grid grid-cols-1 sm:grid-cols-3 gap-3">
                        <div>
                            <label class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300 mb-1">Téléphone 1 (Principal) *</label>
                            <input type="text" id="patientTel1" required placeholder="0550 00 00 00" class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 outline-none focus:border-brand-neon bg-slate-50 dark:bg-black/30 font-medium text-slate-800 dark:text-white">
                        </div>
                        <div>
                            <label class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300 mb-1">Téléphone 2 (Secondaire)</label>
                            <input type="text" id="patientTel2" placeholder="0660 00 00 00" class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 outline-none focus:border-brand-neon bg-slate-50 dark:bg-black/30 text-slate-800 dark:text-white">
                        </div>
                        <div>
                            <label class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300 mb-1">Courriel</label>
                            <input type="email" id="patientEmail" placeholder="patient@mail.dz" class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 outline-none focus:border-brand-neon bg-slate-50 dark:bg-black/30 text-slate-800 dark:text-white">
                        </div>
                    </div>
                </div>

                <!-- 4. LOCALISATION (58 WILAYAS D'ALGÉRIE) -->
                <div>
                    <h4 class="text-xs font-bold uppercase tracking-wider text-slate-400 mb-3 flex items-center gap-1.5">
                        <i class="ph-bold ph-map-pin"></i> Adresse & Région (Algérie)
                    </h4>
                    <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
                        <div>
                            <label class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300 mb-1">Wilaya (58 Wilayas) *</label>
                            <select id="patientWilaya" required onchange="handleWilayaChange()" class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 outline-none focus:border-brand-neon bg-slate-50 dark:bg-black/30 font-bold text-slate-800 dark:text-white">
                                {wilayas_options}
                            </select>
                        </div>
                        <div>
                            <label class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300 mb-1">Commune *</label>
                            <select id="patientCommune" required class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 outline-none focus:border-brand-neon bg-slate-50 dark:bg-black/30 font-medium text-slate-800 dark:text-white"></select>
                        </div>
                    </div>
                </div>

                <!-- 5. PIÈCES D'IDENTITÉ & DOCUMENTS SCANNÉS -->
                <div>
                    <div class="flex items-center justify-between mb-3">
                        <h4 class="text-xs font-bold uppercase tracking-wider text-slate-400 flex items-center gap-1.5">
                            <i class="ph-bold ph-files"></i> Pièces d'Identité & Documents Numérisés
                        </h4>
                        <div class="flex items-center space-x-2">
                            <label for="patientDocUploadInput" class="cursor-pointer bg-white dark:bg-[#121c15] text-slate-700 dark:text-slate-300 border border-slate-200 dark:border-white/10 px-3 py-1.5 rounded-xl text-xs font-bold shadow-sm inline-flex items-center gap-1.5 hover:bg-slate-50 dark:hover:bg-white/5 transition">
                                <i class="ph-bold ph-upload text-brand-neon"></i><span>Téléverser</span>
                            </label>
                            <input type="file" id="patientDocUploadInput" accept="image/*,.pdf" class="hidden" onchange="previewPatientDoc(event)">

                            <button type="button" onclick="openScannerDeviceModal()" class="bg-brand-neon text-black px-3.5 py-1.5 rounded-xl text-xs font-bold shadow-sm inline-flex items-center gap-1.5 hover:opacity-90 transition">
                                <i class="ph-bold ph-printer"></i><span>Scanner Doc (WIA)</span>
                            </button>
                        </div>
                    </div>

                    <div class="bg-slate-50 dark:bg-black/30 p-4 rounded-3xl border border-slate-200/80 dark:border-white/10 space-y-3">
                        <div class="w-full sm:w-72">
                            <label class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300 mb-1">Type de Document</label>
                            <select id="patientTypePiece" class="w-full text-xs p-2 rounded-xl border border-slate-200 dark:border-white/10 outline-none bg-white dark:bg-black/40 text-slate-800 dark:text-white font-medium">
                                {pieces_options}
                            </select>
                        </div>
                        <div id="scannedPiecesGallery" class="grid grid-cols-2 sm:grid-cols-4 gap-3 pt-2"></div>
                    </div>
                </div>

                <div class="flex items-center justify-end space-x-3 pt-4 border-t border-slate-100 dark:border-white/5">
                    <button type="button" onclick="closePatientModal()" class="px-4 py-2 rounded-xl text-slate-400 hover:text-white text-xs font-semibold">Annuler</button>
                    <button type="submit" class="px-6 py-2.5 rounded-xl bg-brand-neon text-black text-xs font-bold shadow-md shadow-brand-neon/20">Enregistrer la Fiche Patient</button>
                </div>
            </form>
        </div>
    </div>

    <!-- MODAL WEBCAM FACE & PROFILS -->
    <div id="cameraCaptureModal" class="fixed inset-0 bg-black/80 backdrop-blur-md flex items-center justify-center hidden z-50 p-4">
        <div class="bg-white dark:bg-[#121c15] rounded-3xl w-full max-w-2xl shadow-2xl border border-slate-100 dark:border-white/10 overflow-hidden flex flex-col max-h-[92vh]">
            <div class="px-6 py-4 border-b border-slate-100 dark:border-white/5 flex justify-between items-center bg-slate-50/50 dark:bg-black/20">
                <div class="flex items-center space-x-2">
                    <div class="w-8 h-8 rounded-xl bg-brand-neon/20 text-brand-neon flex items-center justify-center font-bold text-sm">
                        <i class="ph-bold ph-camera"></i>
                    </div>
                    <h3 class="text-base font-bold text-slate-800 dark:text-white">Capture Photos (Face & Profils)</h3>
                </div>
                <button type="button" onclick="closeCameraCaptureModal()" class="text-slate-400 hover:text-white p-1 rounded-lg">
                    <i class="ph-bold ph-x text-lg"></i>
                </button>
            </div>

            <div class="p-6 space-y-4 overflow-y-auto">
                <div>
                    <label class="block text-xs font-bold text-slate-700 dark:text-slate-300 mb-1">Caméra Détectée</label>
                    <select id="cameraDeviceSelect" onchange="startWebcamStream()" class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/30 font-bold text-slate-800 dark:text-white outline-none"></select>
                </div>

                <div class="relative bg-black rounded-2xl overflow-hidden aspect-video flex items-center justify-center shadow-inner">
                    <video id="webcamVideoFeed" autoplay playsinline class="w-full h-full object-cover"></video>
                    <canvas id="webcamCanvas" class="hidden"></canvas>
                </div>

                <div class="grid grid-cols-3 gap-3 pt-2">
                    <button type="button" onclick="takeSnapshot('face')" class="bg-brand-neon text-black py-2.5 rounded-xl text-xs font-bold shadow-sm inline-flex items-center justify-center gap-1.5">
                        <i class="ph-bold ph-camera"></i>
                        <span>📸 Vue Face</span>
                    </button>
                    <button type="button" onclick="takeSnapshot('pdroit')" class="bg-slate-800 text-white py-2.5 rounded-xl text-xs font-bold shadow-sm inline-flex items-center justify-center gap-1.5">
                        <i class="ph-bold ph-user"></i>
                        <span>📸 Profil Droit</span>
                    </button>
                    <button type="button" onclick="takeSnapshot('pgauche')" class="bg-slate-800 text-white py-2.5 rounded-xl text-xs font-bold shadow-sm inline-flex items-center justify-center gap-1.5">
                        <i class="ph-bold ph-user"></i>
                        <span>📸 Profil Gauche</span>
                    </button>
                </div>
            </div>

            <div class="px-6 py-3 bg-slate-50 dark:bg-black/20 border-t border-slate-100 dark:border-white/5 flex justify-end">
                <button type="button" onclick="closeCameraCaptureModal()" class="px-5 py-2 rounded-xl bg-brand-neon text-black font-bold text-xs shadow-md">Valider et Fermer</button>
            </div>
        </div>
    </div>

    <!-- MODAL LECTEUR BIOMÉTRIQUE EMPREINTE -->
    <div id="fingerprintDeviceModal" class="fixed inset-0 bg-black/70 backdrop-blur-sm flex items-center justify-center hidden z-50 p-4">
        <div class="bg-white dark:bg-[#121c15] rounded-3xl w-full max-w-md shadow-2xl border border-slate-100 dark:border-white/10 overflow-hidden">
            <div class="px-6 py-4 border-b border-slate-100 dark:border-white/5 flex justify-between items-center bg-slate-50/50 dark:bg-black/20">
                <div class="flex items-center space-x-2">
                    <div class="w-8 h-8 rounded-xl bg-brand-neon/20 text-brand-neon flex items-center justify-center font-bold text-sm">
                        <i class="ph-bold ph-fingerprint"></i>
                    </div>
                    <h3 class="text-base font-bold text-slate-800 dark:text-white">Numérisation Empreinte (Index)</h3>
                </div>
                <button type="button" onclick="closeFingerprintDeviceModal()" class="text-slate-400 hover:text-white p-1 rounded-lg">
                    <i class="ph-bold ph-x text-lg"></i>
                </button>
            </div>

            <div class="p-6 space-y-4">
                <div id="fingerprintDeviceSelectionBox">
                    <label class="block text-xs font-bold text-slate-700 dark:text-slate-300 mb-1.5">Lecteur Biométrique Détecté</label>
                    <select id="fingerprintDeviceSelect" class="w-full text-xs p-3 rounded-2xl border border-slate-200 dark:border-white/10 outline-none bg-slate-50 dark:bg-black/30 font-bold text-slate-800 dark:text-white"></select>
                </div>

                <div id="noFingerprintAlert" class="hidden p-3.5 rounded-2xl bg-amber-500/10 border border-amber-500/20 text-amber-500 text-xs font-medium space-y-1">
                    <div class="font-bold flex items-center gap-1.5">⚠️ Aucun lecteur d'empreintes détecté</div>
                    <p class="text-[11px] leading-snug">Branchez un lecteur biométrique USB (DigitalPersona, ZKTeco, SecuGen...) puis réessayez.</p>
                </div>

                <div class="bg-slate-50 dark:bg-black/30 p-4 rounded-2xl border border-slate-200 dark:border-white/10 flex flex-col items-center justify-center space-y-2.5">
                    <div class="w-24 h-28 rounded-2xl bg-white dark:bg-black/40 border-2 border-dashed border-brand-neon/40 flex items-center justify-center shadow-inner relative overflow-hidden">
                        <img id="fingerprintModalLivePreview" src="/static/images/fingerprint-placeholder.svg?w=120&auto=format&fit=crop&q=80" class="w-16 h-20 object-contain opacity-30">
                        <div id="fingerprintScanWave" class="hidden absolute inset-x-0 top-0 h-1 bg-brand-neon shadow-lg shadow-brand-neon/50 animate-bounce"></div>
                    </div>
                    <span id="fingerprintPromptText" class="text-xs font-bold text-slate-700 dark:text-slate-300">Placez l'index sur le capteur</span>
                </div>

                <div class="flex items-center justify-end space-x-2.5 pt-2 border-t border-slate-100 dark:border-white/5">
                    <button type="button" onclick="closeFingerprintDeviceModal()" class="px-4 py-2 rounded-xl text-slate-400 hover:text-white text-xs font-semibold">Annuler</button>
                    <button type="button" id="btnStartFingerprintScan" onclick="executeRealFingerprintScan()" class="px-5 py-2.5 rounded-xl bg-brand-neon text-black text-xs font-bold shadow-md inline-flex items-center gap-1.5 transition">
                        <i class="ph-bold ph-fingerprint text-base"></i>
                        <span>Démarrer le Scan</span>
                    </button>
                </div>
            </div>
        </div>
    </div>

    <!-- MODAL SCANNEUR WIA -->
    <div id="scannerDeviceModal" class="fixed inset-0 bg-black/70 backdrop-blur-sm flex items-center justify-center hidden z-[10000] p-4" role="dialog" aria-modal="true" aria-labelledby="scannerDeviceTitle">
        <div class="bg-white dark:bg-[#121c15] rounded-3xl w-full max-w-md shadow-2xl border border-slate-100 dark:border-white/10 overflow-hidden">
            <div class="px-6 py-4 border-b border-slate-100 dark:border-white/5 flex justify-between items-center bg-slate-50/50 dark:bg-black/20">
                <div class="flex items-center space-x-2">
                    <div class="w-8 h-8 rounded-xl bg-brand-neon/20 text-brand-neon flex items-center justify-center font-bold text-sm">
                        <i class="ph-bold ph-printer"></i>
                    </div>
                    <h3 id="scannerDeviceTitle" class="text-base font-bold text-slate-800 dark:text-white">Scanneur Physique Détecté</h3>
                </div>
                <button type="button" onclick="closeScannerDeviceModal()" class="text-slate-400 hover:text-white p-1 rounded-lg">
                    <i class="ph-bold ph-x text-lg"></i>
                </button>
            </div>

            <div class="p-6 space-y-4">
                <div id="scannerSelectionBox">
                    <label class="block text-xs font-bold text-slate-700 dark:text-slate-300 mb-1.5">Scanneur WIA Connecté</label>
                    <select id="scannerDeviceSelect" class="w-full text-xs p-3 rounded-2xl border border-slate-200 dark:border-white/10 outline-none bg-slate-50 dark:bg-black/30 font-bold text-slate-800 dark:text-white"></select>
                </div>

                <div id="noScannerAlert" class="hidden p-3.5 rounded-2xl bg-amber-500/10 border border-amber-500/20 text-amber-500 text-xs font-medium space-y-1">
                    <div class="font-bold flex items-center gap-1.5">⚠️ Aucun scanneur WIA détecté</div>
                    <p class="text-[11px] leading-snug">Vérifiez la connexion USB de votre scanneur puis réessayez.</p>
                </div>

                <div id="scannerStatusMsg" class="hidden text-center py-2 text-xs font-bold text-brand-neon">
                    ⏳ Numérisation en cours... Veuillez patienter.
                </div>

                <div class="flex items-center justify-end space-x-2.5 pt-2 border-t border-slate-100 dark:border-white/5">
                    <button type="button" onclick="closeScannerDeviceModal()" class="px-4 py-2 rounded-xl text-slate-400 hover:text-white text-xs font-semibold">Annuler</button>
                    <button type="button" id="btnLaunchScan" onclick="executeRealHardwareScan()" class="px-5 py-2.5 rounded-xl bg-brand-neon text-black text-xs font-bold shadow-md inline-flex items-center gap-1.5 transition">
                        <i class="ph-bold ph-scan text-base"></i>
                        <span>Lancer le Scan</span>
                    </button>
                </div>
            </div>
        </div>
    </div>

    <!-- SCRIPT JS COMPLET AVEC PAGINATION PAR 15 -->
    <script>
        (function() {{
            const ALGERIA_WILAYAS_DATA = {wilayas_json};
            let allPatientsList = [];
            let filteredPatientsList = [];
            let currentPatientPage = 1;
            const PATIENTS_PER_PAGE = 15;

            let scannedPiecesList = [];
            let currentMediaStream = null;

            window.loadPatientsModule = async function() {{
                try {{
                    const res = await fetch('/api/patients-module');
                    const data = await res.json();
                    allPatientsList = data.patients || [];
                    document.getElementById('totalPatientsCount').innerText = data.total_count || allPatientsList.length;

                    filterPatients();
                    handleWilayaChange();
                }} catch (err) {{
                    console.error("Erreur de chargement des patients :", err);
                }}
            }};

            window.filterPatients = function() {{
                const q = (document.getElementById('searchPatientInput').value || '').toLowerCase().trim();
                if (!q) {{
                    filteredPatientsList = allPatientsList;
                }} else {{
                    filteredPatientsList = allPatientsList.filter(p => 
                        (p.nom && p.nom.toLowerCase().includes(q)) || 
                        (p.prenom && p.prenom.toLowerCase().includes(q)) || 
                        (p.telephone_1 && p.telephone_1.includes(q)) || 
                        (p.telephone_2 && p.telephone_2.includes(q)) || 
                        (p.wilaya && p.wilaya.toLowerCase().includes(q)) || 
                        (p.commune && p.commune.toLowerCase().includes(q)) || 
                        String(p.id).includes(q)
                    );
                }}
                currentPatientPage = 1;
                renderPatientsTable();
            }};

            window.renderPatientsTable = function() {{
                const tbody = document.getElementById('patients-table-body');
                tbody.innerHTML = '';

                const totalItems = filteredPatientsList.length;
                const totalPages = Math.ceil(totalItems / PATIENTS_PER_PAGE) || 1;

                if (currentPatientPage > totalPages) currentPatientPage = totalPages;
                if (currentPatientPage < 1) currentPatientPage = 1;

                const startIndex = (currentPatientPage - 1) * PATIENTS_PER_PAGE;
                const endIndex = startIndex + PATIENTS_PER_PAGE;
                const pageItems = filteredPatientsList.slice(startIndex, endIndex);

                if (pageItems.length === 0) {{
                    tbody.innerHTML = '<tr><td colspan="8" class="text-center py-8 text-slate-400">Aucun patient trouvé.</td></tr>';
                }} else {{
                    pageItems.forEach(p => {{
                        const tr = document.createElement('tr');
                        tr.className = "hover:bg-slate-50/80 dark:hover:bg-white/[0.02] transition-colors";

                        const piecesCount = (p.pieces_scannees && p.pieces_scannees.length > 0) 
                            ? p.pieces_scannees.length 
                            : (p.doc_identite ? 1 : 0);

                        const docBadge = piecesCount > 0
                            ? `<span class="px-2.5 py-1 rounded-xl text-[10px] font-bold bg-brand-neon/20 text-emerald-800 dark:text-brand-neon border border-brand-neon/30">✓ ${{piecesCount}} pièce(s)</span>`
                            : `<span class="text-slate-400 text-[10px] italic">Non numérisé</span>`;

                        tr.innerHTML = `
                            <td class="py-3.5 pl-2 text-slate-400 font-mono text-[11px]">#DOS-${{String(p.id).padStart(4, '0')}}</td>
                            <td class="py-3.5 flex items-center space-x-3">
                                <img src="${{p.photo_face || p.avatar || '/static/images/avatar-placeholder.svg?w=120&auto=format&fit=crop&q=80'}}" class="w-9 h-9 rounded-full object-cover border border-slate-200 dark:border-white/10 shadow-sm">
                                <div>
                                    <div class="font-bold text-slate-800 dark:text-slate-100 text-xs">${{p.nom}} ${{p.prenom}}</div>
                                    <div class="text-[10px] text-slate-400 font-medium">${{p.age}} (${{p.date_naissance}})</div>
                                </div>
                            </td>
                            <td class="py-3.5 text-slate-700 dark:text-slate-300 font-medium">${{p.genre}}</td>
                            <td class="py-3.5 text-slate-600 dark:text-slate-400">
                                <div class="font-semibold text-slate-800 dark:text-slate-200 text-[11px]">${{p.telephone_1}}</div>
                                ${{p.telephone_2 ? `<div class="text-[10px] text-slate-400 font-mono">${{p.telephone_2}}</div>` : ''}}
                            </td>
                            <td class="py-3.5 text-slate-700 dark:text-slate-300">
                                <div class="font-bold text-[11px]">${{p.wilaya}}</div>
                                <div class="text-[10px] text-slate-400">${{p.commune}}</div>
                            </td>
                            <td class="py-3.5"><span class="px-2.5 py-1 rounded-xl text-[10px] font-extrabold bg-rose-50 dark:bg-rose-950/40 text-rose-600 dark:text-rose-300 border border-rose-200 dark:border-rose-900/40">${{p.groupe_sanguin}}</span></td>
                            <td class="py-3.5">${{docBadge}}</td>
                            <td class="py-3.5 pr-2 text-right">
                                <div class="inline-flex items-center gap-1.5">
                                    <button onclick="editPatient(${{p.id}})" class="p-1.5 text-sky-500 hover:bg-sky-50 dark:hover:bg-sky-500/10 rounded-lg"><i class="ph-bold ph-pencil-simple text-sm"></i></button>
                                    <button onclick="deletePatient(${{p.id}})" class="p-1.5 text-rose-500 hover:bg-rose-50 dark:hover:bg-rose-500/10 rounded-lg"><i class="ph-bold ph-trash text-sm"></i></button>
                                </div>
                            </td>
                        `;
                        tbody.appendChild(tr);
                    }});
                }}

                // GESTION DE LA BARRE DE PAGINATION
                const paginationWrapper = document.getElementById('patientPaginationWrapper');
                const info = document.getElementById('patientPaginationInfo');
                const controls = document.getElementById('patientPaginationControls');

                if (totalItems > 0) {{
                    paginationWrapper.classList.remove('hidden');
                    info.innerHTML = `Affichage de <strong>${{startIndex + 1}}</strong> à <strong>${{Math.min(endIndex, totalItems)}}</strong> sur <strong>${{totalItems}}</strong> patients`;

                    if (totalItems > PATIENTS_PER_PAGE) {{
                        let html = '';
                        const prevDisabled = currentPatientPage <= 1 ? 'opacity-40 cursor-not-allowed' : 'hover:bg-slate-100 dark:hover:bg-white/10 cursor-pointer';
                        html += `<button onclick="setPatientPage(${{currentPatientPage - 1}})" ${{currentPatientPage <= 1 ? 'disabled' : ''}} class="px-3 py-1 rounded-xl border border-slate-200 dark:border-white/10 text-xs font-semibold ${{prevDisabled}}">◀ Précédent</button>`;

                        for (let i = 1; i <= totalPages; i++) {{
                            if (i === 1 || i === totalPages || (i >= currentPatientPage - 1 && i <= currentPatientPage + 1)) {{
                                const activeClass = i === currentPatientPage 
                                    ? 'bg-brand-neon text-black font-bold shadow-sm' 
                                    : 'border border-slate-200 dark:border-white/10 hover:bg-slate-100 dark:hover:bg-white/5 text-slate-700 dark:text-slate-300';
                                html += `<button onclick="setPatientPage(${{i}})" class="w-7 h-7 rounded-xl text-xs flex items-center justify-center transition ${{activeClass}}">${{i}}</button>`;
                            }} else if (i === currentPatientPage - 2 || i === currentPatientPage + 2) {{
                                html += `<span class="px-1 text-slate-400">...</span>`;
                            }}
                        }}

                        const nextDisabled = currentPatientPage >= totalPages ? 'opacity-40 cursor-not-allowed' : 'hover:bg-slate-100 dark:hover:bg-white/10 cursor-pointer';
                        html += `<button onclick="setPatientPage(${{currentPatientPage + 1}})" ${{currentPatientPage >= totalPages ? 'disabled' : ''}} class="px-3 py-1 rounded-xl border border-slate-200 dark:border-white/10 text-xs font-semibold ${{nextDisabled}}">Suivant ▶</button>`;
                        controls.innerHTML = html;
                    }} else {{
                        controls.innerHTML = '';
                    }}
                }} else {{
                    paginationWrapper.classList.add('hidden');
                }}
            }};

            window.setPatientPage = function(p) {{
                currentPatientPage = p;
                renderPatientsTable();
            }};

            window.calculatePatientAge = function() {{
                const dobInput = document.getElementById('patientDob');
                const display = document.getElementById('patientAgeDisplay');
                if (!dobInput || !dobInput.value) return;

                const birthDate = new Date(dobInput.value);
                const today = new Date();

                let years = today.getFullYear() - birthDate.getFullYear();
                let months = today.getMonth() - birthDate.getMonth();

                if (today.getDate() < birthDate.getDate()) months--;
                if (months < 0) {{ years--; months += 12; }}

                if (years < 10) {{
                    display.value = years <= 0 ? `${{months}} mois` : `${{years}} an${{years > 1 ? 's' : ''}} ${{months}} mois`;
                }} else {{
                    display.value = `${{years}} ans`;
                }}
            }};

            window.handleWilayaChange = function() {{
                const wilayaSelect = document.getElementById('patientWilaya');
                const communeSelect = document.getElementById('patientCommune');
                const selected = wilayaSelect.value || "16 - Alger";
                const communes = ALGERIA_WILAYAS_DATA[selected] || ["Centre-Ville"];

                communeSelect.innerHTML = communes.map(c => `<option value="${{c}}">${{c}}</option>`).join('');
            }};

            window.openPatientModal = function() {{
                document.getElementById('patientForm').reset();
                document.getElementById('patientId').value = '';
                document.getElementById('patientAvatarData').value = '';
                document.getElementById('patientPhotoFaceData').value = '';
                document.getElementById('patientPhotoPDroitData').value = '';
                document.getElementById('patientPhotoPGaucheData').value = '';
                document.getElementById('patientEmpreinteData').value = '';
                document.getElementById('patientDocData').value = '';
                document.getElementById('patientAgeDisplay').value = '';
                scannedPiecesList = [];

                const defaultAvatar = '/static/images/avatar-placeholder.svg?w=120&auto=format&fit=crop&q=80';
                document.getElementById('previewPhotoFace').src = defaultAvatar;
                document.getElementById('previewPhotoPDroit').src = defaultAvatar;
                document.getElementById('previewPhotoPGauche').src = defaultAvatar;
                document.getElementById('previewEmpreinteImg').src = '/static/images/fingerprint-placeholder.svg?w=80&auto=format&fit=crop&q=80';

                document.getElementById('labelPhotoFace').innerText = "Photo principale";
                document.getElementById('labelPhotoPDroit').innerText = "Non capturé";
                document.getElementById('labelPhotoPGauche').innerText = "Non capturé";
                document.getElementById('labelEmpreinteStatus').innerText = "Non numérisée";
                document.getElementById('patientModalTitle').innerText = "Ajouter un Nouveau Patient";

                renderScannedPiecesGallery();
                handleWilayaChange();
                document.getElementById('patientModal').classList.remove('hidden');
            }};

            window.closePatientModal = function() {{
                document.getElementById('patientModal').classList.add('hidden');
                // Effacer le raccordement passage si la création a été annulée.
                window.onPatientCreatedForPassage = null;
            }};

            window.editPatient = function(id) {{
                const p = allPatientsList.find(x => x.id === id);
                if (!p) return;

                openPatientModal();

                document.getElementById('patientId').value = p.id;
                document.getElementById('patientNom').value = p.nom;
                document.getElementById('patientPrenom').value = p.prenom;
                document.getElementById('patientDob').value = p.date_naissance;
                calculatePatientAge();
                document.getElementById('patientGenre').value = p.genre || 'Homme';
                document.getElementById('patientTel1').value = p.telephone_1;
                document.getElementById('patientTel2').value = p.telephone_2 || '';
                document.getElementById('patientEmail').value = p.email || '';
                document.getElementById('patientWilaya').value = p.wilaya;
                handleWilayaChange();
                document.getElementById('patientCommune').value = p.commune;
                document.getElementById('patientGroupeSanguin').value = p.groupe_sanguin || 'O+';
                document.getElementById('patientTypePiece').value = p.type_piece_identite || 'Carte Chifa';

                if (p.photo_face) {{
                    document.getElementById('patientPhotoFaceData').value = p.photo_face;
                    document.getElementById('previewPhotoFace').src = p.photo_face;
                    document.getElementById('labelPhotoFace').innerText = "✓ Capturée";
                }}
                if (p.photo_profil_droit) {{
                    document.getElementById('patientPhotoPDroitData').value = p.photo_profil_droit;
                    document.getElementById('previewPhotoPDroit').src = p.photo_profil_droit;
                    document.getElementById('previewPhotoPDroit').classList.remove('opacity-40');
                    document.getElementById('labelPhotoPDroit').innerText = "✓ Capturée";
                }}
                if (p.photo_profil_gauche) {{
                    document.getElementById('patientPhotoPGaucheData').value = p.photo_profil_gauche;
                    document.getElementById('previewPhotoPGauche').src = p.photo_profil_gauche;
                    document.getElementById('previewPhotoPGauche').classList.remove('opacity-40');
                    document.getElementById('labelPhotoPGauche').innerText = "✓ Capturée";
                }}
                if (p.empreinte_index) {{
                    document.getElementById('patientEmpreinteData').value = p.empreinte_index;
                    document.getElementById('previewEmpreinteImg').src = p.empreinte_index;
                    document.getElementById('previewEmpreinteImg').classList.remove('opacity-30');
                    document.getElementById('labelEmpreinteStatus').innerText = "✓ Index Enregistré";
                }}

                scannedPiecesList = p.pieces_scannees || [];
                renderScannedPiecesGallery();
                document.getElementById('patientModalTitle').innerText = `Modifier Dossier #${{p.id}} : ${{p.nom}} ${{p.prenom}}`;
            }};

            window.handlePatientSubmit = async function(e) {{
                e.preventDefault();

                const payload = {{
                    id: document.getElementById('patientId').value ? parseInt(document.getElementById('patientId').value) : null,
                    nom: document.getElementById('patientNom').value.trim(),
                    prenom: document.getElementById('patientPrenom').value.trim(),
                    date_naissance: document.getElementById('patientDob').value,
                    genre: document.getElementById('patientGenre').value,
                    telephone_1: document.getElementById('patientTel1').value.trim(),
                    telephone_2: document.getElementById('patientTel2').value.trim(),
                    email: document.getElementById('patientEmail').value.trim(),
                    wilaya: document.getElementById('patientWilaya').value,
                    commune: document.getElementById('patientCommune').value,
                    groupe_sanguin: document.getElementById('patientGroupeSanguin').value,
                    type_piece_identite: document.getElementById('patientTypePiece').value,
                    doc_identite: document.getElementById('patientDocData').value,
                    avatar: document.getElementById('patientAvatarData').value || document.getElementById('patientPhotoFaceData').value,
                    photo_face: document.getElementById('patientPhotoFaceData').value,
                    photo_profil_droit: document.getElementById('patientPhotoPDroitData').value,
                    photo_profil_gauche: document.getElementById('patientPhotoPGaucheData').value,
                    empreinte_index: document.getElementById('patientEmpreinteData').value,
                    pieces_scannees: scannedPiecesList
                }};

                const res = await fetch('/api/patients-module/save', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify(payload)
                }}).then(r => r.json());

                if (res.success) {{
                    const passageCreationCallback = !payload.id && window.onPatientCreatedForPassage;
                    closePatientModal();
                    await loadPatientsModule();
                    if (typeof passageCreationCallback === 'function') {{
                        await passageCreationCallback({{ ...payload, id: res.patient_id }});
                    }}
                    alert(res.message);
                }} else {{
                    alert(res.message);
                }}
            }};

            window.deletePatient = async function(id) {{
                if (confirm("Voulez-vous supprimer ce dossier patient ?")) {{
                    await fetch('/api/patients-module/delete', {{
                        method: 'POST',
                        headers: {{ 'Content-Type': 'application/json' }},
                        body: JSON.stringify({{ id }})
                    }});
                    loadPatientsModule();
                }}
            }};

            // Biométrie : Webcam
            window.openCameraCaptureModal = async function() {{
                document.getElementById('cameraCaptureModal').classList.remove('hidden');
                await detectSystemCameras();
                startWebcamStream();
            }};

            window.closeCameraCaptureModal = function() {{
                stopWebcamStream();
                document.getElementById('cameraCaptureModal').classList.add('hidden');
            }};

            async function detectSystemCameras() {{
                const select = document.getElementById('cameraDeviceSelect');
                select.innerHTML = '';
                try {{
                    const devices = await navigator.mediaDevices.enumerateDevices();
                    const videoDevices = devices.filter(d => d.kind === 'videoinput');
                    if (videoDevices.length > 0) {{
                        select.innerHTML = videoDevices.map((d, i) => `<option value="${{d.deviceId}}">📷 ${{d.label || 'Caméra ' + (i + 1)}}</option>`).join('');
                    }} else {{
                        select.innerHTML = '<option value="">Caméra par défaut</option>';
                    }}
                }} catch (e) {{
                    select.innerHTML = '<option value="">Caméra standard</option>';
                }}
            }}

            window.startWebcamStream = async function() {{
                stopWebcamStream();
                const select = document.getElementById('cameraDeviceSelect');
                const deviceId = select ? select.value : '';
                const constraints = deviceId ? {{ video: {{ deviceId: {{ exact: deviceId }} }} }} : {{ video: true }};

                try {{
                    currentMediaStream = await navigator.mediaDevices.getUserMedia(constraints);
                    document.getElementById('webcamVideoFeed').srcObject = currentMediaStream;
                }} catch (err) {{
                    console.warn("Webcam non accessible:", err);
                }}
            }};

            function stopWebcamStream() {{
                if (currentMediaStream) {{
                    currentMediaStream.getTracks().forEach(t => t.stop());
                    currentMediaStream = null;
                }}
            }}

            window.takeSnapshot = function(angle) {{
                const video = document.getElementById('webcamVideoFeed');
                const canvas = document.getElementById('webcamCanvas');
                canvas.width = video.videoWidth || 640;
                canvas.height = video.videoHeight || 480;
                canvas.getContext('2d').drawImage(video, 0, 0, canvas.width, canvas.height);
                const b64 = canvas.toDataURL('image/jpeg', 0.9);

                if (angle === 'face') {{
                    document.getElementById('patientPhotoFaceData').value = b64;
                    document.getElementById('patientAvatarData').value = b64;
                    document.getElementById('previewPhotoFace').src = b64;
                    document.getElementById('labelPhotoFace').innerText = "✓ Capturée";
                }} else if (angle === 'pdroit') {{
                    document.getElementById('patientPhotoPDroitData').value = b64;
                    document.getElementById('previewPhotoPDroit').src = b64;
                    document.getElementById('previewPhotoPDroit').classList.remove('opacity-40');
                    document.getElementById('labelPhotoPDroit').innerText = "✓ Capturée";
                }} else if (angle === 'pgauche') {{
                    document.getElementById('patientPhotoPGaucheData').value = b64;
                    document.getElementById('previewPhotoPGauche').src = b64;
                    document.getElementById('previewPhotoPGauche').classList.remove('opacity-40');
                    document.getElementById('labelPhotoPGauche').innerText = "✓ Capturée";
                }}
            }};

            // Biométrie : Empreinte
            window.openFingerprintDeviceModal = async function() {{
                const modal = document.getElementById('fingerprintDeviceModal');
                const selectEl = document.getElementById('fingerprintDeviceSelect');
                modal.classList.remove('hidden');

                try {{
                    const res = await fetch('/api/patients-module/fingerprint/detect').then(r => r.json());
                    const devices = res.devices || [];
                    if (devices.length > 0) {{
                        selectEl.innerHTML = devices.map(d => `<option value="${{d.id}}">🖐️ ${{d.name}}</option>`).join('');
                    }} else {{
                        selectEl.innerHTML = '<option value="default">🖐️ Capteur Biométrique Windows (WBF)</option>';
                    }}
                }} catch (e) {{
                    selectEl.innerHTML = '<option value="default">🖐️ Capteur USB</option>';
                }}
            }};

            window.closeFingerprintDeviceModal = function() {{
                document.getElementById('fingerprintDeviceModal').classList.add('hidden');
            }};

            window.executeRealFingerprintScan = async function() {{
                const wave = document.getElementById('fingerprintScanWave');
                const promptText = document.getElementById('fingerprintPromptText');
                wave.classList.remove('hidden');
                promptText.innerText = "⏳ Numérisation en cours...";

                try {{
                    const res = await fetch('/api/patients-module/fingerprint/acquire', {{
                        method: 'POST',
                        headers: {{ 'Content-Type': 'application/json' }},
                        body: JSON.stringify({{ device_id: document.getElementById('fingerprintDeviceSelect').value }})
                    }}).then(r => r.json());

                    if (res.success && res.fingerprint_data) {{
                        document.getElementById('patientEmpreinteData').value = res.fingerprint_data;
                        document.getElementById('previewEmpreinteImg').src = res.fingerprint_data;
                        document.getElementById('previewEmpreinteImg').classList.remove('opacity-30');
                        document.getElementById('labelEmpreinteStatus').innerText = "✓ Index Numérisé";
                        setTimeout(() => {{ closeFingerprintDeviceModal(); }}, 600);
                    }}
                }} catch (err) {{
                    alert("Erreur biométrique.");
                }} finally {{
                    wave.classList.add('hidden');
                }}
            }};

            // Scanneur WIA Physique
            window.openScannerDeviceModal = async function() {{
                const scannerModal = document.getElementById('scannerDeviceModal');
                scannerModal.style.zIndex = '10000';
                scannerModal.classList.remove('hidden');
                requestAnimationFrame(() => scannerModal.querySelector('#scannerDeviceSelect')?.focus());
                const select = document.getElementById('scannerDeviceSelect');
                select.innerHTML = '<option value="">Détection en cours...</option>';

                try {{
                    const bridge = window.pywebview?.api;
                    if (!bridge?.client_scanner_detect) throw new Error('Ouvrez Cliniva avec le client Cliniva de ce poste pour utiliser son scanner.');
                    const res = await bridge.client_scanner_detect();
                    const scanners = res.scanners || [];
                    if (scanners.length > 0) {{
                        select.innerHTML = scanners.map(s => `<option value="${{s.id}}">🖨️ ${{s.name}}</option>`).join('');
                    }} else {{
                        document.getElementById('noScannerAlert').classList.remove('hidden');
                    }}
                }} catch (e) {{
                    document.getElementById('noScannerAlert').classList.remove('hidden');
                    const hint = document.querySelector('#noScannerAlert p');
                    if (hint) hint.textContent = e.message || 'Scanner local indisponible. Vérifiez son branchement et son pilote WIA.';
                }}
            }};

            window.closeScannerDeviceModal = function() {{
                document.getElementById('scannerDeviceModal').classList.add('hidden');
            }};

            window.executeRealHardwareScan = async function() {{
                const statusMsg = document.getElementById('scannerStatusMsg');
                statusMsg.classList.remove('hidden');

                try {{
                    const bridge = window.pywebview?.api;
                    if (!bridge?.client_scanner_acquire) throw new Error('Le scanner client nécessite le lanceur Cliniva sur ce poste.');
                    const res = await bridge.client_scanner_acquire(document.getElementById('scannerDeviceSelect').value);

                    if (res.success && res.image_data) {{
                        const typeDoc = document.getElementById('patientTypePiece').value;
                        scannedPiecesList.push({{
                            type: typeDoc,
                            date: new Date().toLocaleDateString('fr-FR'),
                            data: res.image_data
                        }});
                        const addedPieceIndex = scannedPiecesList.length - 1;
                        renderScannedPiecesGallery();
                        closeScannerDeviceModal();
                        scrollPatientScannedPieceIntoView(addedPieceIndex);
                    }} else {{
                        alert(res.message || "Erreur de numérisation.");
                    }}
                }} catch (err) {{
                    alert(err.message || "Erreur lors du scan.");
                }} finally {{
                    statusMsg.classList.add('hidden');
                }}
            }};

            window.previewPatientAvatar = function(event) {{
                const file = event.target.files[0];
                if (file) {{
                    const reader = new FileReader();
                    reader.onload = function(e) {{
                        document.getElementById('patientAvatarData').value = e.target.result;
                        document.getElementById('patientPhotoFaceData').value = e.target.result;
                        document.getElementById('previewPhotoFace').src = e.target.result;
                        document.getElementById('labelPhotoFace').innerText = "✓ Fichier chargé";
                    }};
                    reader.readAsDataURL(file);
                }}
            }};

            window.previewPatientDoc = function(event) {{
                const file = event.target.files[0];
                const typeDoc = document.getElementById('patientTypePiece').value;
                if (file) {{
                    const reader = new FileReader();
                    reader.onload = function(e) {{
                        scannedPiecesList.push({{
                            type: typeDoc,
                            date: new Date().toLocaleDateString('fr-FR'),
                            data: e.target.result
                        }});
                        const addedPieceIndex = scannedPiecesList.length - 1;
                        renderScannedPiecesGallery();
                        scrollPatientScannedPieceIntoView(addedPieceIndex);
                    }};
                    reader.readAsDataURL(file);
                }}
            }};

            function renderScannedPiecesGallery() {{
                const container = document.getElementById('scannedPiecesGallery');
                container.innerHTML = '';

                if (scannedPiecesList.length === 0) {{
                    container.innerHTML = '<span class="text-xs text-slate-400 italic col-span-4 text-center py-2">Aucun document scanné.</span>';
                    return;
                }}

                scannedPiecesList.forEach((piece, index) => {{
                    const div = document.createElement('div');
                    div.id = `patientScannedPiece-${{index}}`;
                    div.className = "bg-white dark:bg-black/30 p-2.5 rounded-2xl border border-slate-200 dark:border-white/10 shadow-sm flex flex-col justify-between relative group";
                    const isPdf = String(piece.data || '').toLowerCase().startsWith('data:application/pdf');

                    div.innerHTML = `
                        <div class="relative overflow-hidden rounded-xl h-20 bg-slate-100 dark:bg-black/50 flex items-center justify-center">
                            ${{isPdf 
                                ? '<i class="ph-bold ph-file-pdf text-3xl text-rose-500"></i>' 
                                : `<img src="${{piece.data}}" class="w-full h-full object-cover">`}}
                        </div>
                        <div class="pt-2">
                            <span class="text-[11px] font-bold text-slate-800 dark:text-slate-200 block truncate">${{piece.type || 'Document'}}</span>
                            <span class="text-[9px] text-slate-400">${{piece.date || ''}}</span>
                        </div>
                        <button type="button" onclick="removeScannedPiece(${{index}})" class="absolute top-1.5 right-1.5 bg-rose-500 text-white w-5 h-5 rounded-full flex items-center justify-center opacity-0 group-hover:opacity-100 transition text-[10px]">✕</button>
                    `;
                    container.appendChild(div);
                }});
            }}

            function scrollPatientScannedPieceIntoView(index) {{
                requestAnimationFrame(() => {{
                    const piece = document.getElementById(`patientScannedPiece-${{index}}`);
                    piece?.scrollIntoView({{ behavior: 'smooth', block: 'center' }});
                    piece?.classList.add('ring-2', 'ring-brand-neon');
                    window.setTimeout(() => piece?.classList.remove('ring-2', 'ring-brand-neon'), 1800);
                }});
            }}

            window.removeScannedPiece = function(index) {{
                scannedPiecesList.splice(index, 1);
                renderScannedPiecesGallery();
            }};

            // Chargement initial
            loadPatientsModule();
        }})();
    </script>
    """
