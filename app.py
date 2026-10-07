@app.route('/')
def arena():
    dev_id = session.get('device_id')
    info = connected_devices.get(dev_id, {"number": 1, "color": "#28a745", "balance": 0})
    return render_template_string(MAIN_TEMPLATE, device_id=dev_id, device_number=info["number"], color=info["color"], balance=info["balance"])

@app.route('/portal')
def portal():
    dev_id = session.get('device_id')
    info = connected_devices.get(dev_id, {"number": 1, "color": "#28a745"})
    return render_template_string(PORTAL_TEMPLATE, device_number=info["number"], color=info["color"])

# Keep the old /arena route as a fallback redirect
@app.route('/arena')
def arena_redirect():
    return redirect(url_for('arena'))
