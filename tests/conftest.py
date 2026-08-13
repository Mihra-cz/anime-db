import os


# Některé testovací moduly importují globální ``app`` z app.main. Její vytvoření
# spouští idempotentní startup sync, proto musí být testovací cíl nastaven dříve,
# než pytest začne testovací moduly importovat.
os.environ["ANIME_PATH"] = "/tmp"
os.environ["DATABASE_URL"] = "sqlite://"
os.environ["METADATA_ARTWORK_DIRECTORY"] = "/tmp/anime-db-test-artwork"
os.environ["METADATA_DOWNLOAD_ARTWORK"] = "false"
