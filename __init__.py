import base64

from flask import Flask, request, jsonify
from uuid import uuid4
from flask_socketio import SocketIO, send, emit, join_room, leave_room, rooms
from time import sleep
from threading import Thread, Lock
from datetime import datetime, timedelta
import json

app = Flask(__name__)
app.config['SECRET_KEY'] = str(uuid4())

SHARDING_FACTOR = 2
REPLICATION_FACTOR = 1


socketio = SocketIO(app, logger=True, engineio_logger=True, async_mode='eventlet')
connected_users = {}
connection_lock = Lock()

# storage dict contains:
# size of shards kept by client on his machine
# size of shards saved by client in network
@socketio.on('connect')
def handle_join(storage_data):
    connection_id = request.sid
    connection_lock.acquire()
    connected_users[connection_id] = {**storage_data}
    connection_lock.release()
    emit('users_count', {'users_count': len(connected_users)})


@socketio.on('disconnect')
def handle_disconnect():
    connection_lock.acquire()
    del connected_users[request.sid]
    amount = len(connected_users)
    connection_lock.release()
    emit('users_count', {'users_count': amount})

# transmission of a shard
# shard_dict should contain this data
# (same as Shard.cs)
# {
#       FileAESKeyEncrypted
#       FilePublicKey
#       ShardIndex
#       ShardBytes
#       PoOBytesUnencrypted
#       PoOBytesEncrypted
#       MD5Checksum
#       ShardCheckLastTime
# }
# Saving algorithm:
# 1. Someone sends his shard
# 2. Server does request to the clients to understand who can be the best option;
# 3. Client saves file and sends "ok" when finished
@socketio.on('save_shard')
def handle_save_shard(shard_dict_string):
    sender_sid = request.sid

    # the string is a valid json
    shard_dict = json.loads(shard_dict_string)

    # transforming to list to sort
    connected_users_list = []
    with connection_lock:
        for user in connected_users.keys():
            connected_users_list.append((user, connected_users[user]['client_kept'], connected_users[user]['client_saved']))

    # the top of the sorted list are the ones who kept the least and saved the most data into the network;
    # they must be our choice
    connected_users_list.sort(key=lambda x: x[1] - x[2] * REPLICATION_FACTOR)

    saved_amount = 0
    for user, _, __ in connected_users_list:
        # we don't reuse the same user to store his data
        #if user == sender_sid:
        # continue

        # the has_shard is a "request" which is needed to check, if the client has some shard of file we're trying to save
        response = socketio.call('has_shard', {'FilePublicKey': shard_dict['FilePublicKey']}, to=user)

        # this already means that we can save the shard
        if not response:
            emit('save_shard', shard_dict, to=user)
            saved_amount += 1
            print('saving a shard to another user')

        # once we've saved to enough nodes, we can say that we've successfully saved a shard
        if saved_amount == REPLICATION_FACTOR:
            emit('shard_saved', {'FilePublicKey': shard_dict['FilePublicKey']}, to=sender_sid)
            break

    if saved_amount < REPLICATION_FACTOR:
        emit('save_shard_ack', {'response': 'saving shard failed: not enough peers', 'success': False}, to=sender_sid)
    else:
        emit('save_shard_ack', {'response': 'saved successfully', 'success': True}, to=sender_sid)


# This msg updates information on user
@socketio.on('saved_size')
def handle_saved_size(sizes_dict):
    sid = request.sid

    with connection_lock:
        connected_users[sid]['client_kept'] = sizes_dict['client_kept']
        connected_users[sid]['client_saved'] = sizes_dict['client_saved']

    emit('saved_size_ack', {'response': 'saved sizes updates', 'success': True}, to=sid)


# this mechanism is designed to proof that owner_sid is truly an owner of file file_public_key
# it makes him decrypt a small 16-bytes message; if it succeeds,
def check_file_ownership(owner_sid, file_public_key) -> bool:
    # there are no blocking broadcast call, so...
    with connection_lock:
        connected_user_sids = connected_users.keys()

    for user in connected_user_sids:
        if user != owner_sid:
            response = socketio.call('has_shard', {'FilePublicKey': file_public_key}, to=user)
            if response:
                # response from client on this moment is base64 string
                enc_proof_bytes_b64 = socketio.call(
                    'get_proof_bytes', {'FilePublicKey': file_public_key}, to=user
                )
                dec_proof_bytes_b64 = socketio.call(
                    'check_proof_bytes', {'FilePublicKey': file_public_key, 'ProofBytes': enc_proof_bytes_b64}, to=owner_sid
                )
                # verdict: is the one who is owner is truly an owner?
                result = socketio.call(
                    'compare_check_bytes', {'FilePublicKey': file_public_key, 'ProofBytesDecrypted': dec_proof_bytes_b64}, to=user
                )
                return result

    return False


# this "route" gets shards and returns it to request sender
@socketio.on('get_file')
def handle_get_file(file_public_key):
    sid = request.sid
    is_owner = check_file_ownership(sid, file_public_key)

    shards = []
    if is_owner:
        for i in range(SHARDING_FACTOR):
            have_found = False
            for user in connected_users.keys():
                response = socketio.call('has_shard', {'FilePublicKey': file_public_key, 'ShardIndex': i}, to=user)
                if response:
                    emit('get_file_ack', {'success': True, 'response': f'Found shard index {i}'}, to=sid)
                    shards.append(socketio.call('get_shard', file_public_key, to=user))
                    have_found = True
                    break
            if not have_found:
                emit('get_file_ack', {'success': False, 'response': f'Not found shard index {i}'}, to=sid)

        emit('get_file', shards, to=sid)


@socketio.on('delete_file')
def handle_delete_file(file_public_key):
    sid = request.sid
    is_owner = check_file_ownership(sid, file_public_key)

    if is_owner:
        emit('delete_shard', file_public_key, broadcast=True)


# has_shard message is already enough for checking file status.
# response message can be just ignored
@socketio.on('check_file')
def handle_check_file(file_public_key):
    emit('has_shard', {'FilePublicKey': file_public_key}, broadcast=True)


if __name__ == '__main__':
    socketio.run(app, port=5000)


