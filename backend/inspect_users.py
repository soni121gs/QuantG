from pymongo import MongoClient
import json

client = MongoClient('mongodb://localhost:27017')
db = client['quantg']
print('collections:', db.list_collection_names())
print('users count:', db.users.count_documents({}))
print(json.dumps([{'id': u['id'], 'email': u['email'], 'created_at': u['created_at']} for u in db.users.find({}, {'_id': 0, 'id': 1, 'email': 1, 'created_at': 1}).limit(20)], indent=2))
