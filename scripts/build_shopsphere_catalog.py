"""
Builds data/shopsphere_catalog.json, the ShopSphere Demo 1 catalog (66 products, 6 of them kids').

    python scripts/build_shopsphere_catalog.py

The base product list below is followed by CURATION: per-product overrides and
the final colours, each tied to the hand-picked Pixabay photo that shows it
(see scripts/fetch_catalog_photos.py). Colours follow the photos available,
so a product only offers colours it has a convincing photo for.
After changing this, rebuild, re-run the photo script, then `python -m backend.db.init_db`.
"""
import collections
import json

C = {  # colour palette: name -> hex
    "Black": "#1c1c1e", "White": "#f5f5f2", "Blue": "#2f5fb3", "Navy": "#1f2a44", "Grey": "#8e9196",
    "Red": "#c0392b", "Brown": "#7a4a2a", "Tan": "#c69c6d", "Beige": "#d9c7a7", "Pink": "#e8a0b4",
    "Green": "#3e7d4f", "Olive": "#6b6b3a", "Yellow": "#e7c33a", "Purple": "#6d4c9f", "Silver": "#c0c3c7",
    "Gold": "#c9a74a", "Cream": "#efe6d2", "Light Blue": "#8fb3d9", "Dark Blue": "#23395d",
    "Emerald": "#1f7a5a", "Midnight": "#1d2533", "Starlight": "#e8dfcf", "Orange": "#e07b39", "Camel": "#b98b5e",
}


def col(*names):
    return [{"name": n, "hex": C[n]} for n in names]


P = []


def slugify(name):
    slug = name.lower()
    for ch in "'\"().,":
        slug = slug.replace(ch, "")
    return "-".join(slug.replace("&", "and").replace("/", " ").split())


def add(pid, name, brand, dept, cat, sub, gender, price, rating, reviews, days, new, sizes, colors,
        desc, tags, query, oos=None, label="Size"):
    slug = slugify(name)
    P.append({
        "product_id": pid, "slug": slug, "name": name, "brand": brand, "department": dept,
        "category": cat, "subcategory": sub, "gender": gender, "price": price, "rating": rating,
        "review_count": reviews, "delivery_days": days, "is_new": new, "option_label": label,
        "sizes": sizes, "colors": colors, "description": desc, "tags": tags, "photo_query": query,
        "out_of_stock": oos or [],
    })


# ── SHOES (16) ────────────────────────────────────────────────────────────────
add("SSP001", "Runner Pro X", "Kinetic", "shoes", "running_shoes", "running", "unisex", 129.0, 4.7, 1284, 3, True,
    "shoe_unisex", col("Black", "White", "Blue"),
    "A cushioned everyday road runner with a responsive foam midsole and breathable knit upper.",
    ["everyday running", "cushioned", "road", "breathable"], "running shoe", [{"size": "11"}])
add("SSP002", "FlexRun 5", "Kinetic", "shoes", "running_shoes", "running", "unisex", 139.0, 4.6, 962, 3, True,
    "shoe_unisex", col("Grey", "Black", "Red"),
    "Lightweight and flexible for everyday miles, with a grippy rubber outsole.",
    ["everyday running", "lightweight", "flexible", "road"], "running shoe", [{"size": "12", "color": "Red"}])
add("SSP003", "Daily Runner", "Northpace", "shoes", "running_shoes", "running", "unisex", 119.0, 4.5, 1530, 4, False,
    "shoe_unisex", col("Navy", "White"),
    "A durable, comfortable trainer built for beginners and everyday runs.",
    ["everyday running", "durable", "beginner", "comfortable"], "running sneaker")
add("SSP004", "Nike Pegasus 41", "Nike", "shoes", "running_shoes", "running", "men", 149.99, 4.4, 3241, 3, False,
    "shoe_men", col("Black", "White"),
    "Nike's versatile road trainer with ReactX foam for daily training.",
    ["road running", "daily training", "cushioned"], "running shoe", [{"size": "13"}])
add("SSP005", "Adidas Ultraboost Light", "Adidas", "shoes", "running_shoes", "running", "women", 189.99, 4.6, 2104, 4, True,
    "shoe_women", col("White", "Pink", "Black"),
    "Energy-returning Boost cushioning in Adidas' lightest Ultraboost yet.",
    ["cushioned", "lightweight", "road running", "premium"], "running shoe")
add("SSP006", "Trail Blazer GTX", "Northpace", "shoes", "running_shoes", "trail", "men", 134.99, 4.3, 688, 5, False,
    "shoe_men", col("Olive", "Black"),
    "A waterproof trail shoe with aggressive lugs for mud and rock.",
    ["trail running", "waterproof", "grip", "hiking"], "trail running shoe")
add("SSP007", "Lumen Cloud Glide", "Lumen", "shoes", "running_shoes", "running", "women", 124.99, 4.4, 845, 4, True,
    "shoe_women", col("White", "Purple", "Grey"),
    "Soft, cloud-like cushioning for easy recovery runs and long walks.",
    ["recovery runs", "cushioned", "walking", "comfortable"], "running shoe")
add("SSP008", "Nike Air Force 1 '07", "Nike", "shoes", "sneakers", "sneakers", "unisex", 114.99, 4.8, 8120, 3, False,
    "shoe_unisex", col("White", "Black"),
    "The iconic basketball-born sneaker with crisp leather and Air cushioning.",
    ["casual", "classic", "leather", "streetwear"], "white sneaker")
add("SSP009", "Adidas Samba OG", "Adidas", "shoes", "sneakers", "sneakers", "unisex", 99.99, 4.7, 5402, 4, True,
    "shoe_unisex", col("White", "Black", "Green"),
    "A terrace classic with a soft leather upper and gum rubber sole.",
    ["casual", "retro", "leather", "streetwear"], "sneaker")
add("SSP010", "Converse Chuck Taylor All Star", "Converse", "shoes", "sneakers", "sneakers", "unisex", 64.99, 4.6, 12890, 4, False,
    "shoe_unisex", col("Black", "White", "Red"),
    "The timeless canvas high-top that goes with everything.",
    ["casual", "canvas", "classic", "high-top"], "canvas sneaker", [{"size": "6", "color": "Red"}])
add("SSP011", "Urban Court Low", "Lumen", "shoes", "sneakers", "sneakers", "women", 79.99, 4.4, 610, 3, True,
    "shoe_women", col("White", "Pink", "Beige"),
    "A clean low-top court sneaker with a cushioned insole.",
    ["casual", "minimal", "everyday", "comfortable"], "women sneaker")
add("SSP012", "Hartwell Chelsea Boot", "Hartwell", "shoes", "boots", "boots", "men", 169.99, 4.6, 1144, 5, False,
    "shoe_men", col("Brown", "Black"),
    "Full-grain leather Chelsea boots with elastic sides and a stacked heel.",
    ["leather", "smart casual", "classic", "pull-on"], "chelsea boot")
add("SSP013", "Ridge Hiker Waterproof Boot", "Northpace", "shoes", "boots", "boots", "unisex", 149.99, 4.5, 932, 5, False,
    "shoe_unisex", col("Brown", "Grey"),
    "Waterproof hiking boots with ankle support and a rugged outsole.",
    ["hiking", "waterproof", "outdoor", "ankle support"], "hiking boot")
add("SSP014", "Coastline Slide Sandal", "Marisol", "shoes", "shoes", "sandals", "women", 44.99, 4.3, 402, 3, True,
    "shoe_women", col("Tan", "Black", "White"),
    "A comfy contoured slide with a soft strap for warm days.",
    ["summer", "casual", "comfortable", "beach"], "sandal")
add("SSP015", "Hartwell Oxford Derby", "Hartwell", "shoes", "shoes", "formal", "men", 139.99, 4.5, 733, 5, False,
    "shoe_men", col("Black", "Brown"),
    "Polished leather derby shoes for the office and formal occasions.",
    ["formal", "leather", "office", "wedding"], "leather dress shoe")
add("SSP016", "Marisol Block Heel Pump", "Marisol", "shoes", "shoes", "formal", "women", 89.99, 4.4, 518, 4, True,
    "shoe_women", col("Black", "Beige", "Red"),
    "A comfortable block-heel pump that works from desk to dinner.",
    ["formal", "office", "heels", "party"], "high heel shoe")

# ── CLOTHING (18) ─────────────────────────────────────────────────────────────
add("SSP017", "Essential Crew Tee (Women)", "Sphere Basics", "clothing", "clothing", "tops", "women", 24.99, 4.5, 2210, 3, False,
    "apparel_women", col("White", "Black", "Pink", "Green"),
    "A soft, relaxed cotton tee for everyday layering.", ["cotton", "basics", "everyday", "casual"], "women t-shirt")
add("SSP018", "Wrap Midi Dress", "Marisol", "clothing", "clothing", "dresses", "women", 69.99, 4.6, 894, 4, True,
    "apparel_women", col("Black", "Emerald", "Red"),
    "A flattering wrap dress with a flowing midi skirt.", ["dress", "party", "date night", "elegant"], "midi dress")
add("SSP019", "Linen Summer Sundress", "Marisol", "clothing", "clothing", "dresses", "women", 54.99, 4.4, 507, 4, True,
    "apparel_women", col("Yellow", "White", "Blue"),
    "Breathable linen sundress, light enough for the hottest days.", ["dress", "summer", "linen", "casual"], "summer dress")
add("SSP020", "High-Rise Skinny Jeans", "Sphere Denim", "clothing", "clothing", "jeans", "women", 59.99, 4.5, 1320, 3, False,
    "jeans_women", col("Light Blue", "Dark Blue", "Black"),
    "Stretch denim skinny jeans with a high, shaping waist.", ["denim", "jeans", "stretch", "everyday"], "women jeans",
    [{"size": "24", "color": "Black"}], "Waist")
add("SSP021", "Levi's 501 Original Jeans", "Levi's", "clothing", "clothing", "jeans", "men", 79.99, 4.7, 9876, 4, False,
    "jeans_men", col("Dark Blue", "Light Blue", "Black"),
    "The original straight-fit button-fly jean since 1873.", ["denim", "jeans", "classic", "straight fit"], "men jeans",
    [{"size": "38"}], "Waist")
add("SSP022", "Oversized Knit Sweater", "Sphere Basics", "clothing", "clothing", "knitwear", "women", 64.99, 4.6, 688, 4, True,
    "apparel_women", col("Cream", "Grey", "Brown"),
    "A cosy chunky-knit sweater with dropped shoulders.", ["knitwear", "cosy", "winter", "casual"], "knit sweater")
add("SSP023", "Quilted Puffer Jacket", "Northline", "clothing", "clothing", "jackets", "women", 119.99, 4.6, 742, 5, True,
    "apparel_women", col("Black", "Beige", "Green"),
    "Lightweight insulated puffer that packs into its own pocket.", ["jacket", "winter", "warm", "packable"], "puffer jacket")
add("SSP024", "Seamless Yoga Leggings", "Pulse Active", "clothing", "clothing", "activewear", "women", 49.99, 4.7, 2640, 3, False,
    "apparel_women", col("Black", "Navy", "Purple"),
    "Squat-proof, sweat-wicking leggings with a high waistband.", ["activewear", "yoga", "gym", "stretch"], "yoga leggings")
add("SSP025", "Performance Sports Bra", "Pulse Active", "clothing", "clothing", "activewear", "women", 34.99, 4.5, 1180, 3, False,
    "apparel_women", col("Black", "Pink", "Blue"),
    "Medium-support sports bra with removable pads.", ["activewear", "gym", "running", "training"], "sports bra")
add("SSP026", "Classic Oxford Shirt", "Hartwell", "clothing", "clothing", "shirts", "men", 59.99, 4.5, 1032, 4, False,
    "apparel_men", col("White", "Light Blue", "Pink"),
    "A crisp button-down Oxford shirt for work or weekends.", ["shirt", "office", "smart casual", "cotton"], "men shirt")
add("SSP027", "Pique Polo Shirt", "Sphere Basics", "clothing", "clothing", "shirts", "men", 44.99, 4.4, 815, 3, False,
    "apparel_men", col("Navy", "White", "Green"),
    "A breathable cotton pique polo with a classic fit.", ["polo", "casual", "summer", "cotton"], "polo shirt")
add("SSP028", "Essential Crew Tee (Men)", "Sphere Basics", "clothing", "clothing", "tops", "men", 22.99, 4.5, 3105, 3, False,
    "apparel_men", col("Black", "White", "Grey", "Navy"),
    "Everyday heavyweight cotton tee with a structured neckline.", ["cotton", "basics", "everyday", "casual"], "men t-shirt")
add("SSP029", "Slim Chino Pants", "Hartwell", "clothing", "clothing", "pants", "men", 54.99, 4.4, 964, 4, False,
    "jeans_men", col("Beige", "Navy", "Olive"),
    "Slim stretch chinos that move from office to weekend.", ["pants", "chinos", "smart casual", "stretch"], "chino pants",
    None, "Waist")
add("SSP030", "Denim Trucker Jacket", "Sphere Denim", "clothing", "clothing", "jackets", "men", 89.99, 4.6, 1210, 4, True,
    "apparel_men", col("Blue", "Black"),
    "A classic trucker jacket in rigid denim that wears in beautifully.", ["jacket", "denim", "casual", "layering"], "denim jacket")
add("SSP031", "Nike Dri-FIT Running Tee", "Nike", "clothing", "clothing", "activewear", "men", 34.99, 4.5, 2780, 3, False,
    "apparel_men", col("Black", "Blue", "Red"),
    "Sweat-wicking Dri-FIT tee built for runs and workouts.", ["activewear", "running", "gym", "sweat-wicking"], "running shirt")
add("SSP032", "Adidas Tiro Track Pants", "Adidas", "clothing", "clothing", "activewear", "unisex", 49.99, 4.6, 4410, 4, False,
    "apparel_unisex", col("Black", "Navy"),
    "Slim tapered track pants with zip pockets and ankle zips.", ["activewear", "training", "track pants", "sporty"], "track pants")
add("SSP033", "Hooded Fleece Sweatshirt", "Sphere Basics", "clothing", "clothing", "tops", "unisex", 59.99, 4.6, 1870, 3, True,
    "apparel_unisex", col("Grey", "Black", "Green"),
    "A brushed-fleece hoodie with a roomy kangaroo pocket.", ["hoodie", "cosy", "casual", "layering"], "hoodie")
add("SSP034", "Rain Shell Jacket", "Northline", "clothing", "clothing", "jackets", "unisex", 99.99, 4.4, 655, 5, False,
    "apparel_unisex", col("Yellow", "Black", "Navy"),
    "A packable waterproof shell with taped seams and a hood.", ["jacket", "waterproof", "rain", "outdoor"], "rain jacket")

# ── ACCESSORIES (12) ──────────────────────────────────────────────────────────
add("SSP035", "Leather Crossbody Bag", "Marisol", "accessories", "bags", "bags", "women", 129.99, 4.6, 904, 4, True,
    "one_size", col("Tan", "Black", "Red"),
    "A compact leather crossbody with an adjustable strap.", ["bag", "leather", "everyday", "crossbody"], "leather handbag")
add("SSP036", "Everyday Canvas Backpack", "Northline", "accessories", "bags", "bags", "unisex", 69.99, 4.5, 1630, 3, False,
    "one_size", col("Navy", "Black", "Grey"),
    "A water-resistant backpack with a padded 15-inch laptop sleeve.", ["backpack", "travel", "laptop", "school"], "backpack")
add("SSP037", "Leather Tote Bag", "Marisol", "accessories", "bags", "bags", "women", 149.99, 4.5, 712, 4, False,
    "one_size", col("Black", "Brown"),
    "A roomy structured tote that fits a laptop and the day's essentials.", ["bag", "leather", "work", "tote"], "tote bag")
add("SSP038", "Ray-Ban Wayfarer Classic", "Ray-Ban", "accessories", "sunglasses", "sunglasses", "unisex", 163.0, 4.7, 6230, 3, False,
    "one_size", col("Black", "Brown"),
    "The legendary Wayfarer frame with G-15 polarized lenses.", ["sunglasses", "classic", "polarized", "summer"], "sunglasses")
add("SSP039", "Aviator Sunglasses", "Sphere Optics", "accessories", "sunglasses", "sunglasses", "unisex", 119.99, 4.5, 980, 3, False,
    "one_size", col("Gold", "Silver"),
    "Lightweight metal aviators with UV400 protection.", ["sunglasses", "aviator", "summer", "metal"], "aviator sunglasses")
add("SSP040", "Minimalist Mesh Watch", "Nordic Time", "accessories", "watches", "watches", "unisex", 99.99, 4.5, 1104, 4, True,
    "one_size", col("Silver", "Black", "Gold"),
    "A slim minimalist watch with a stainless mesh strap.", ["watch", "minimal", "gift", "stainless steel"], "wrist watch")
add("SSP041", "Classic Leather Strap Watch", "Nordic Time", "accessories", "watches", "watches", "men", 129.99, 4.6, 846, 4, False,
    "one_size", col("Brown", "Black"),
    "A clean dial on a genuine leather strap, water-resistant to 50 m.", ["watch", "leather", "classic", "gift"], "leather watch")
add("SSP042", "Reversible Leather Belt", "Hartwell", "accessories", "accessories", "belts", "men", 39.99, 4.4, 1290, 3, False,
    "belt", col("Black", "Brown"),
    "One belt, two looks: reversible black and brown leather.", ["belt", "leather", "office", "reversible"], "leather belt")
add("SSP043", "Nike Heritage86 Cap", "Nike", "accessories", "accessories", "caps", "unisex", 26.99, 4.6, 3320, 3, False,
    "one_size", col("Black", "White", "Navy"),
    "A low-profile cotton cap with an adjustable strap.", ["cap", "casual", "sporty", "sun"], "baseball cap")
add("SSP044", "Wool Beanie", "Northline", "accessories", "accessories", "hats", "unisex", 24.99, 4.5, 1410, 3, True,
    "one_size", col("Grey", "Black", "Red"),
    "A warm ribbed wool-blend beanie.", ["beanie", "winter", "warm", "knit"], "beanie hat")
add("SSP045", "Cashmere-Blend Scarf", "Marisol", "accessories", "accessories", "scarves", "women", 49.99, 4.6, 688, 4, True,
    "one_size", col("Camel", "Grey", "Red"),
    "A soft, generous cashmere-blend scarf.", ["scarf", "winter", "cashmere", "gift"], "scarf")
add("SSP046", "Everyday Leather Wallet", "Hartwell", "accessories", "accessories", "wallets", "men", 44.99, 4.4, 970, 3, False,
    "one_size", col("Black", "Brown"),
    "A slim bifold wallet in full-grain leather with RFID blocking.", ["wallet", "leather", "gift", "rfid"], "leather wallet")

# ── ELECTRONICS (14) ──────────────────────────────────────────────────────────
add("SSP047", "Sony WH-1000XM5 Headphones", "Sony", "electronics", "electronics", "headphones", "unisex", 399.99, 4.8, 7840, 3, False,
    "one_size", col("Black", "Silver"),
    "Industry-leading noise cancelling with 30-hour battery life.",
    ["headphones", "noise cancelling", "wireless", "travel"], "wireless headphones")
add("SSP048", "Bose QuietComfort Ultra Headphones", "Bose", "electronics", "electronics", "headphones", "unisex", 429.0, 4.7, 3920, 3, True,
    "one_size", col("Black", "White"),
    "Immersive spatial audio with world-class noise cancellation.",
    ["headphones", "noise cancelling", "wireless", "premium"], "over-ear headphones")
add("SSP049", "Apple AirPods Pro (2nd Gen)", "Apple", "electronics", "electronics", "earbuds", "unisex", 249.0, 4.8, 15420, 2, False,
    "one_size", col("White"),
    "Active noise cancellation, adaptive audio and a USB-C MagSafe case.",
    ["earbuds", "noise cancelling", "wireless", "apple"], "wireless earbuds")
add("SSP050", "Pulse Buds Pro", "Pulse Audio", "electronics", "electronics", "earbuds", "unisex", 89.99, 4.4, 1260, 3, True,
    "one_size", col("Black", "White", "Blue"),
    "Noise-cancelling earbuds with 32 hours of total battery.",
    ["earbuds", "noise cancelling", "wireless", "budget"], "earbuds")
add("SSP051", "Samsung Galaxy Buds3 Pro", "Samsung", "electronics", "electronics", "earbuds", "unisex", 249.99, 4.5, 2210, 3, True,
    "one_size", col("Silver", "White"),
    "Hi-fi sound with intelligent ANC and a sleek blade design.",
    ["earbuds", "noise cancelling", "wireless", "android"], "wireless earbuds")
add("SSP052", "Apple iPhone 15", "Apple", "electronics", "phones", "phones", "unisex", 799.0, 4.8, 11230, 2, False,
    "phone_storage", col("Black", "Blue", "Pink"),
    "Dynamic Island, a 48MP camera and USB-C.", ["phone", "smartphone", "camera", "apple"], "smartphone",
    [{"size": "512GB", "color": "Pink"}], "Storage")
add("SSP053", "Samsung Galaxy S24", "Samsung", "electronics", "phones", "phones", "unisex", 799.99, 4.7, 6840, 2, False,
    "phone_storage_2", col("Black", "Purple", "Yellow"),
    "Galaxy AI built in, with a bright 6.2-inch display.", ["phone", "smartphone", "android", "camera"], "smartphone",
    None, "Storage")
add("SSP054", "Apple MacBook Air 13-inch (M3)", "Apple", "electronics", "laptops", "laptops", "unisex", 1099.0, 4.8, 5320, 3, True,
    "laptop_storage", col("Midnight", "Silver", "Starlight"),
    "Strikingly thin, fanless, with up to 18 hours of battery life.", ["laptop", "apple", "lightweight", "work"], "laptop",
    None, "Storage")
add("SSP055", "Aero 14 Laptop", "Sphere Tech", "electronics", "laptops", "laptops", "unisex", 899.0, 4.5, 980, 4, False,
    "laptop_storage_2", col("Grey", "Silver"),
    "A 14-inch aluminium ultrabook with a 2.8K display.", ["laptop", "work", "lightweight", "student"], "laptop",
    None, "Storage")
add("SSP056", "Apple Watch Series 9", "Apple", "electronics", "watches", "smartwatches", "unisex", 399.0, 4.7, 7710, 2, False,
    "watch_case", col("Midnight", "Pink", "Silver"),
    "A brighter display, double-tap gesture and advanced health sensors.", ["smartwatch", "fitness", "health", "apple"],
    "smartwatch", None, "Case size")
add("SSP057", "Garmin Forerunner 265", "Garmin", "electronics", "watches", "smartwatches", "unisex", 449.99, 4.7, 2890, 3, False,
    "one_size", col("Black", "White"),
    "A GPS running watch with an AMOLED display and training readiness.", ["smartwatch", "running", "gps", "fitness"], "sport watch")
add("SSP058", "Pulse Fit Band", "Pulse Audio", "electronics", "electronics", "fitness_trackers", "unisex", 59.99, 4.3, 1540, 3, True,
    "one_size", col("Black", "Pink", "Blue"),
    "A slim fitness band that tracks steps, sleep and heart rate.", ["fitness tracker", "health", "sleep", "budget"], "fitness tracker")
add("SSP059", "JBL Flip 6 Speaker", "JBL", "electronics", "electronics", "speakers", "unisex", 129.99, 4.7, 9020, 3, False,
    "one_size", col("Black", "Blue", "Red"),
    "A rugged, waterproof portable speaker with bold JBL sound.", ["speaker", "bluetooth", "waterproof", "portable"], "bluetooth speaker")
add("SSP060", "Sphere Sound Mini", "Sphere Tech", "electronics", "electronics", "speakers", "unisex", 39.99, 4.3, 760, 3, True,
    "one_size", col("Grey", "Blue"),
    "A palm-sized Bluetooth speaker with 12-hour battery.", ["speaker", "bluetooth", "portable", "budget"], "small speaker")


# ── KIDS (6) ──────────────────────────────────────────────────────────────────
# A small kids' section (gender "kids"): fictional house brands, kids' sizing (C = little kids,
# Y = big kids; clothing by age). No photos yet: the cards and pages show the product without one
# until a hand-picked photo is added to CURATION (then run scripts/fetch_catalog_photos.py).
add("SSP061", "Kinetic Junior Runner", "Kinetic", "shoes", "running_shoes", "running", "kids", 59.99, 4.6, 412, 3, True,
    "shoe_kids", col("Blue", "Pink", "Black"),
    "A light, cushioned kids' running shoe with a hook-and-loop strap and a grippy rubber sole for school and play.",
    ["kids", "boys", "girls", "running", "easy on", "school"], "kids running shoe")
add("SSP062", "Northpace Kids Trail Scout", "Northpace", "shoes", "running_shoes", "trail", "kids", 54.99, 4.5, 238, 4, False,
    "shoe_kids", col("Green", "Navy"),
    "A sturdy kids' trail shoe with toe protection and deep lugs for parks, trails and muddy days.",
    ["kids", "boys", "girls", "trail", "outdoor", "durable"], "kids trail shoe")
add("SSP063", "Sphere Kids Light-Up Sneaker", "Sphere", "shoes", "sneakers", "sneakers", "kids", 44.99, 4.7, 655, 3, True,
    "shoe_kids", col("White", "Pink", "Black"),
    "A canvas kids' sneaker with a light-up sole and an elastic lace so little ones can slip it on themselves.",
    ["kids", "boys", "girls", "light-up", "casual", "slip-on"], "kids sneaker")
add("SSP064", "Kids Graphic Crew Tee", "Sphere Basics", "clothing", "clothing", "tops", "kids", 16.99, 4.4, 321, 3, False,
    "apparel_kids", col("Yellow", "Blue", "Red"),
    "A soft cotton crew-neck tee with a playful print, made to survive the wash.",
    ["kids", "boys", "girls", "cotton", "t-shirt", "everyday"], "kids t-shirt")
add("SSP065", "Kids Zip Fleece Hoodie", "Northpace", "clothing", "clothing", "tops", "kids", 29.99, 4.6, 289, 4, True,
    "apparel_kids", col("Navy", "Purple", "Grey"),
    "A warm brushed-fleece hoodie with a full zip and pockets, for chilly mornings and the playground.",
    ["kids", "boys", "girls", "fleece", "hoodie", "warm"], "kids hoodie")
add("SSP066", "Kids School Backpack", "Sphere", "accessories", "bags", "bags", "kids", 34.99, 4.5, 207, 3, False,
    "one_size", col("Blue", "Pink", "Green"),
    "A light 15-litre school backpack with padded straps, a water-bottle pocket and reflective trim.",
    ["kids", "boys", "girls", "school", "backpack", "lightweight"], "kids backpack")

CURATION = {'SSP001': {'colors': [['Black', {'pixabay_id': 115151, 'mode': 'largest'}],
                       ['White', {'pixabay_id': 77085, 'mode': 'studio'}],
                       ['Blue', {'pixabay_id': 5351339, 'mode': 'studio'}]]},
 'SSP002': {'colors': [['Orange', {'pixabay_id': 2768733, 'mode': 'largest', 'kernel': 25}],
                       ['Green', {'pixabay_id': 2768760, 'mode': 'largest', 'kernel': 25}]],
            'out_of_stock': [{'size': '12', 'color': 'Green'}]},
 'SSP003': {'colors': [['Blue', {'pixabay_id': 371625, 'mode': 'studio'}],
                       ['Pink', {'pixabay_id': 371624, 'mode': 'studio'}]]},
 'SSP004': {'colors': [['Coral', {'pixabay_id': 1324431, 'mode': 'studio'}],
                       ['Teal', {'pixabay_id': 2799608, 'mode': 'studio'}]]},
 'SSP005': {'colors': [['Grey', {'pixabay_id': 2554690, 'mode': 'studio'}],
                       ['White', {'pixabay_id': 5418991, 'mode': 'studio'}]]},
 'SSP006': {'colors': [['Green', {'pixabay_id': 629643, 'mode': 'studio'}],
                       ['Black', {'pixabay_id': 5351330, 'mode': 'studio'}]]},
 'SSP007': {'colors': [['White', {'pixabay_id': 321199, 'mode': 'studio'}],
                       ['Silver', {'pixabay_id': 77087, 'mode': 'studio'}]]},
 'SSP008': {'name': 'Sphere Canvas Low',
            'brand': 'Sphere Basics',
            'price': 49.99,
            'rating': 4.4,
            'review_count': 1180,
            'description': 'A simple canvas low-top with a cushioned footbed.',
            'tags': ['casual', 'canvas', 'everyday', 'lightweight'],
            'colors': [['Navy', {'pixabay_id': 2743420, 'mode': 'largest', 'kernel': 25}]]},
 'SSP009': {'name': 'Adidas NMD_R1',
            'price': 149.99,
            'rating': 4.6,
            'review_count': 4870,
            'description': 'Boost-cushioned street sneaker with a sock-like knit upper.',
            'tags': ['casual', 'streetwear', 'cushioned', 'knit'],
            'colors': [['Black', {'pixabay_id': 5418990, 'mode': 'studio'}]]},
 'SSP010': {'colors': [['Green', {'pixabay_id': 2768263, 'mode': 'largest', 'kernel': 25}],
                       ['Red', {'pixabay_id': 2768218, 'mode': 'largest', 'kernel': 25}],
                       ['Purple', {'pixabay_id': 2768260, 'mode': 'largest', 'kernel': 25}]]},
 'SSP011': {'colors': [['White', {'pixabay_id': 4762266, 'mode': 'studio'}],
                       ['Lilac', {'pixabay_id': 2732693, 'mode': 'studio'}]]},
 'SSP012': {'colors': [['Brown', {'pixabay_id': 3743542, 'mode': 'studio'}]]},
 'SSP013': {'colors': [['Navy', {'pixabay_id': 220499, 'mode': 'studio'}],
                       ['Brown', {'pixabay_id': 3583017, 'mode': 'studio'}]]},
 'SSP014': {'name': 'Coastline Flip-Flop',
            'price': 24.99,
            'description': 'Soft, lightweight flip-flops for the beach and lazy weekends.',
            'colors': [['Light Blue', {'pixabay_id': 4370860, 'mode': 'studio'}],
                       ['Pink', {'pixabay_id': 251019, 'mode': 'studio'}]]},
 'SSP015': {'colors': [['Tan', {'pixabay_id': 6078951, 'mode': 'studio'}],
                       ['Brown', {'pixabay_id': 2434210, 'mode': 'studio'}]]},
 'SSP016': {'name': 'Marisol Classic Pump',
            'description': 'A timeless pointed pump that works from desk to dinner.',
            'colors': [['Red', {'pixabay_id': 20085, 'mode': 'studio'}],
                       ['Black', {'pixabay_id': 1460033, 'mode': 'studio'}]]},
 'SSP017': {'name': 'Raglan Baseball Tee',
            'subcategory': 'tops',
            'description': 'A soft cotton raglan tee with contrast sleeves.',
            'tags': ['cotton', 'casual', 'sporty', 'everyday'],
            'colors': [['Red', {'pixabay_id': 5206369, 'mode': 'studio'}],
                       ['Peach', {'pixabay_id': 5206371, 'mode': 'studio'}]]},
 'SSP018': {'name': 'Tiered Denim Midi Dress',
            'brand': 'Sphere Denim',
            'description': 'A relaxed tiered midi dress in soft washed denim.',
            'tags': ['dress', 'denim', 'casual', 'everyday'],
            'colors': [['Blue', {'pixabay_id': 8568399, 'mode': 'studio'}]]},
 'SSP019': {'colors': [['White', {'pixabay_id': 107269, 'mode': 'studio'}]]},
 'SSP020': {'colors': [['Black', {'pixabay_id': 4514891, 'mode': 'studio'}],
                       ['Blue', {'pixabay_id': 4514892, 'mode': 'studio'}]]},
 'SSP021': {'colors': [['Light Blue', {'pixabay_id': 2799490, 'mode': 'studio'}],
                       ['Grey', {'pixabay_id': 4514889, 'mode': 'studio'}]]},
 'SSP022': {'colors': [['Beige', {'pixabay_id': 3847148, 'mode': 'studio'}],
                       ['Rose', {'pixabay_id': 3847119, 'mode': 'studio'}]]},
 'SSP023': {'name': 'Quilted Moto Jacket',
            'description': 'A quilted jacket with snap details and a stand collar.',
            'tags': ['jacket', 'quilted', 'layering', 'smart casual'],
            'colors': [['Black', {'pixabay_id': 2821961, 'mode': 'studio'}]]},
 'SSP024': {'name': 'Ribbed Turtleneck Top',
            'brand': 'Sphere Basics',
            'subcategory': 'tops',
            'price': 39.99,
            'rating': 4.5,
            'description': 'A fitted ribbed turtleneck that layers under everything.',
            'tags': ['knitwear', 'layering', 'winter', 'basics'],
            'colors': [['Grey', {'pixabay_id': 3847132, 'mode': 'studio'}]]},
 'SSP025': {'name': 'Breton Stripe Sweater',
            'brand': 'Hartwell',
            'gender': 'men',
            'subcategory': 'knitwear',
            'sizes': 'apparel_men',
            'price': 69.99,
            'rating': 4.5,
            'description': 'A classic striped crew-neck in soft cotton knit.',
            'tags': ['knitwear', 'striped', 'classic', 'casual'],
            'colors': [['Grey', {'pixabay_id': 3740357, 'mode': 'studio'}]]},
 'SSP026': {'colors': [['White', {'pixabay_id': 3740340, 'mode': 'studio'}],
                       ['Pink', {'pixabay_id': 3740347, 'mode': 'studio'}],
                       ['Navy', {'pixabay_id': 2947549, 'mode': 'studio'}]]},
 'SSP027': {'colors': [['Red', {'pixabay_id': 3183681, 'mode': 'studio'}]]},
 'SSP028': {'colors': [['Black', {'pixabay_id': 1886001, 'mode': 'largest', 'kernel': 17}]]},
 'SSP029': {'colors': [['Black', {'pixabay_id': 7866, 'mode': 'studio'}]]},
 'SSP030': {'colors': [['Light Blue', {'pixabay_id': 2566082, 'mode': 'studio'}]]},
 'SSP031': {'name': 'Printed Camp Shirt',
            'brand': 'Hartwell',
            'subcategory': 'shirts',
            'price': 49.99,
            'rating': 4.4,
            'description': 'A relaxed short-sleeve camp shirt with an all-over print.',
            'tags': ['shirt', 'summer', 'printed', 'casual'],
            'colors': [['Navy', {'pixabay_id': 2563851, 'mode': 'studio'}]]},
 'SSP032': {'name': 'Knit Fringe Poncho',
            'brand': 'Marisol',
            'gender': 'women',
            'subcategory': 'knitwear',
            'sizes': 'one_size',
            'price': 54.99,
            'rating': 4.5,
            'description': 'An open-knit poncho with a fringed hem.',
            'tags': ['knitwear', 'boho', 'layering', 'summer'],
            'colors': [['Cream', {'pixabay_id': 2619832, 'mode': 'studio'}]]},
 'SSP033': {'colors': [['Beige', {'pixabay_id': 9260884, 'mode': 'studio'}]]},
 'SSP034': {'name': 'Satin Evening Gown',
            'brand': 'Marisol',
            'gender': 'women',
            'subcategory': 'dresses',
            'sizes': 'apparel_women',
            'price': 189.0,
            'rating': 4.7,
            'description': 'A floor-length satin gown with a thigh-high slit.',
            'tags': ['dress', 'evening', 'formal', 'party'],
            'colors': [['Red', {'pixabay_id': 5660012, 'mode': 'studio'}]]},
 'SSP035': {'colors': [['Red', {'pixabay_id': 2661412, 'mode': 'studio'}],
                       ['Navy', {'pixabay_id': 1051714, 'mode': 'studio'}]]},
 'SSP036': {'name': 'Trailhead Hiking Backpack',
            'description': 'A 30L hiking pack with a padded hip belt and rain cover.',
            'tags': ['backpack', 'hiking', 'travel', 'outdoor'],
            'colors': [['Blue', {'pixabay_id': 139758, 'mode': 'studio'}]]},
 'SSP037': {'name': 'Structured Leather Handbag',
            'description': 'A structured top-handle handbag with a detachable strap.',
            'tags': ['bag', 'leather', 'work', 'handbag'],
            'colors': [['Black', {'pixabay_id': 444171, 'mode': 'studio'}],
                       ['Coral', {'pixabay_id': 229610, 'mode': 'studio'}]]},
 'SSP038': {'colors': [['Grey', {'pixabay_id': 2110273, 'mode': 'studio'}]]},
 'SSP039': {'colors': [['Black', {'pixabay_id': 3688030, 'mode': 'studio'}]]},
 'SSP040': {'colors': [['Gold', {'pixabay_id': 4895499, 'mode': 'studio'}]]},
 'SSP041': {'colors': [['Brown', {'pixabay_id': 4895498, 'mode': 'studio'}],
                       ['Silver', {'pixabay_id': 4623183, 'mode': 'studio'}]]},
 'SSP042': {'name': 'Classic Leather Belt',
            'description': 'A full-grain leather belt with a brushed metal buckle.',
            'tags': ['belt', 'leather', 'office', 'classic'],
            'colors': [['Black', {'pixabay_id': 2146914, 'mode': 'studio'}],
                       ['Brown', {'pixabay_id': 139757, 'mode': 'studio'}]]},
 'SSP043': {'name': 'Marisol Cat-Eye Sunglasses',
            'brand': 'Marisol',
            'gender': 'women',
            'category': 'sunglasses',
            'subcategory': 'sunglasses',
            'price': 89.99,
            'rating': 4.5,
            'description': 'Retro cat-eye frames with gradient UV400 lenses.',
            'tags': ['sunglasses', 'retro', 'summer', 'cat-eye'],
            'colors': [['Black', {'pixabay_id': 178153, 'mode': 'studio'}],
                       ['Brown', {'pixabay_id': 789897, 'mode': 'studio'}]]},
 'SSP044': {'name': 'Evening Clutch Bag',
            'brand': 'Marisol',
            'gender': 'women',
            'category': 'bags',
            'subcategory': 'bags',
            'price': 59.99,
            'rating': 4.6,
            'description': 'A metallic clasp clutch for evenings out.',
            'tags': ['bag', 'evening', 'party', 'clutch'],
            'colors': [['Gold', {'pixabay_id': 215451, 'mode': 'studio'}]]},
 'SSP045': {'name': 'Silk Print Scarf',
            'description': 'A lightweight silk scarf in a painterly print.',
            'tags': ['scarf', 'silk', 'gift', 'printed'],
            'colors': [['Blue', {'pixabay_id': 281442, 'mode': 'studio'}]]},
 'SSP046': {'colors': [['Black', {'pixabay_id': 1934036, 'mode': 'studio'}],
                       ['Brown', {'pixabay_id': 1081310, 'mode': 'studio'}]]},
 'SSP047': {'colors': [['Black', {'pixabay_id': 5596988, 'mode': 'studio'}],
                       ['White', {'pixabay_id': 3661771, 'mode': 'studio'}]]},
 'SSP048': {'name': 'Pulse Studio Wireless Headphones',
            'brand': 'Pulse Audio',
            'price': 179.99,
            'rating': 4.5,
            'review_count': 2140,
            'description': 'Comfortable over-ear wireless headphones with 40-hour battery.',
            'tags': ['headphones', 'wireless', 'comfortable', 'travel'],
            'colors': [['Navy', {'pixabay_id': 15600, 'mode': 'studio'}],
                       ['Brown', {'pixabay_id': 1868612, 'mode': 'studio'}]]},
 'SSP049': {'colors': [['White', {'pixabay_id': 5193970, 'mode': 'studio'}]]},
 'SSP050': {'colors': [['Silver', {'pixabay_id': 5598952, 'mode': 'studio'}]]},
 'SSP051': {'name': 'Pulse Reference Open-Back Headphones',
            'brand': 'Pulse Audio',
            'subcategory': 'headphones',
            'price': 299.99,
            'rating': 4.6,
            'description': 'Open-back studio headphones with a wide, natural soundstage.',
            'tags': ['headphones', 'studio', 'audiophile', 'wired'],
            'colors': [['Black', {'pixabay_id': 7721777, 'mode': 'studio'}],
                       ['Silver', {'pixabay_id': 4602717, 'mode': 'studio'}]]},
 'SSP052': {'colors': [['Green', {'pixabay_id': 7481400, 'mode': 'studio'}],
                       ['Silver', {'pixabay_id': 3068617, 'mode': 'largest'}]],
            'out_of_stock': [{'size': '512GB', 'color': 'Green'}]},
 'SSP053': {'colors': [['Silver', {'pixabay_id': 1844848, 'mode': 'studio'}],
                       ['Grey', {'pixabay_id': 2104312, 'mode': 'studio'}]]},
 'SSP054': {'colors': [['Silver', {'pixabay_id': 1282241, 'mode': 'largest'}],
                       ['Gold', {'pixabay_id': 3737334, 'mode': 'studio'}]]},
 'SSP055': {'colors': [['Silver', {'pixabay_id': 5029868, 'mode': 'studio'}],
                       ['Graphite', {'pixabay_id': 5141242, 'mode': 'studio'}]]},
 'SSP056': {'colors': [['Tan', {'pixabay_id': 2569185, 'mode': 'studio'}]]},
 'SSP057': {'name': 'Pulse Smartwatch S2',
            'brand': 'Sphere Tech',
            'price': 199.99,
            'rating': 4.4,
            'review_count': 1320,
            'description': 'A square-face smartwatch with GPS, heart rate and 5-day battery.',
            'tags': ['smartwatch', 'fitness', 'gps', 'notifications'],
            'colors': [['Black', {'pixabay_id': 889639, 'mode': 'studio'}]]},
 'SSP058': {'colors': [['White', {'pixabay_id': 2059937, 'mode': 'studio'}]]},
 'SSP059': {'colors': [['Black', {'pixabay_id': 5800162, 'mode': 'studio'}],
                       ['Red', {'pixabay_id': 5077789, 'mode': 'studio'}]]},
 'SSP060': {'colors': [['Blue', {'pixabay_id': 2557531, 'mode': 'studio'}],
                       ['Pink', {'pixabay_id': 2557528, 'mode': 'studio'}]]}}

# Kids' products: no hand-picked photo yet (photo None: shown without a photo).
CURATION.update({pid: {"colors": [[c["name"], None] for c in next(p for p in P if p["product_id"] == pid)["colors"]]}
                 for pid in ("SSP061", "SSP062", "SSP063", "SSP064", "SSP065", "SSP066")})

EXTRA_COLORS = {'Peach': '#f2b28c', 'Teal': '#2bb3b1', 'Coral': '#f26b5b', 'Lilac': '#b7a3d8', 'Rose': '#b5838d', 'Graphite': '#4a4d52'}
C.update(EXTRA_COLORS)

for p in P:
    cfg = CURATION[p["product_id"]]
    for key, value in cfg.items():
        if key == "colors":
            p["colors"] = [{"name": n, "hex": C[n], "photo": photo} for n, photo in value]
        else:
            p[key] = value
    p.pop("photo_query", None)
    p["slug"] = slugify(p["name"])  # renamed products get a matching URL slug

catalog = {
    "merchant": {"merchant_id": "SHOPSPHERE", "merchant_name": "ShopSphere", "trust_status": "trusted", "tier": "local"},
    "size_ranges": {
        "shoe_unisex": ["6", "7", "8", "9", "10", "11", "12"],
        "shoe_men": ["7", "8", "9", "10", "11", "12", "13"],
        "shoe_women": ["5", "6", "7", "8", "9", "10"],
        "shoe_kids": ["11C", "12C", "13C", "1Y", "2Y", "3Y", "4Y", "5Y", "6Y"],
        "apparel_kids": ["4", "5", "6", "7", "8", "10", "12", "14"],
        "apparel_women": ["XS", "S", "M", "L", "XL"],
        "apparel_men": ["S", "M", "L", "XL", "XXL"],
        "apparel_unisex": ["XS", "S", "M", "L", "XL", "XXL"],
        "jeans_women": ["24", "26", "28", "30", "32"],
        "jeans_men": ["28", "30", "32", "34", "36", "38"],
        "belt": ["S", "M", "L", "XL"],
        "one_size": [None],
        "watch_case": ["41mm", "45mm"],
        "phone_storage": ["128GB", "256GB", "512GB"],
        "phone_storage_2": ["128GB", "256GB"],
        "laptop_storage": ["256GB", "512GB"],
        "laptop_storage_2": ["512GB", "1TB"],
    },
    "products": P,
}

assert len(P) == 66
assert len({p["product_id"] for p in P}) == 66 and len({p["slug"] for p in P}) == 66
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "data" / "shopsphere_catalog.json"
with open(OUT, "w", encoding="utf-8") as f:
    json.dump(catalog, f, indent=1, ensure_ascii=False)

print("products:", len(P), dict(collections.Counter(p["department"] for p in P)))
variants = sum(len(catalog["size_ranges"][p["sizes"]]) * len(p["colors"]) for p in P)
print("variants:", variants, "| colour photos needed:", sum(len(p["colors"]) for p in P))
print("genders:", dict(collections.Counter(p["gender"] for p in P)))
real = {"Nike", "Adidas", "Converse", "Levi's", "Ray-Ban", "Sony", "Bose", "Apple", "Samsung", "Garmin", "JBL"}
print("real-brand products:", sum(p["brand"] in real for p in P), f"/ {len(P)}")
