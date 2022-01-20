import json
import os
import pprint
import sys
import uuid

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.cloud import pubsub_v1

from database import JsonDatabase

PROJECT_ID          = os.environ['GOOGLE_CLOUD_PROJECT']
PUBSUB_SUBSCRIPTION = 'codelab'
PROCUREMENT_API     = 'cloudcommerceprocurement'


def _generate_internal_account_id():
    ### TODO: Replace with whatever ID generation code already exists. ###
    return str(uuid.uuid4())


class Procurement(object):
    """Utilities for interacting with the Procurement API."""
    
    def __init__(self, database):
        self.service = build(PROCUREMENT_API, 'v1', cache_discovery=False)
        self.database = database
    
    ##########################
    ### Account operations ###
    ##########################
    
    def _get_account_id(self, name):
        return name[len(f'providers/DEMO-{PROJECT_ID}/accounts/'):]
    
    def _get_account_name(self, account_id):
        return f'providers/DEMO-{PROJECT_ID}/accounts/{account_id}'
    
    def get_account(self, account_id):
        """Gets an account from the Procurement Service."""
        name = self._get_account_name(account_id)
        request = self.service.providers().accounts().get(name=name)
        try:
            response = request.execute()
            return response
        except HttpError as err:
            if err.resp.status == 404:
                return None
    
    def approve_account(self, account_id):
        try:
            """Approves the account in the Procurement Service."""
            name = self._get_account_name(account_id)
            request = self.service.providers().accounts().approve(
                name=name, body={'approvalName': 'signup'})
            request.execute()
            print('\n[ INFO ] Account Approved\n')
        except Exception as e:
            print(f'\n[ WARNING ] Account NOT approved. {e}\n') 
    
    def handle_account_message(self, message):
        """Handles incoming Pub/Sub messages about account resources."""
        
        account_id = message['id']
        
        customer = self.database.read(account_id)
        account = self.get_account(account_id)
        
        print(f'\n[ INFO ] Account: {json.dumps(account,indent=4)}\n')
        
        ############################## IMPORTANT ##############################
        ### In true integrations, Pub/Sub messages for new accounts should  ###
        ### be ignored. Account approvals are granted as a one-off action   ###
        ### during customer sign up. This codelab does not include the sign ###
        ### up flow, so it chooses to approve accounts here instead.        ###
        ### Production code for real, non-codelab services should never     ###
        ### blindly approve these. The following should be done as a result ###
        ### of a user signing up.                                           ###
        #######################################################################
        if account:
            approval = None
            for account_approval in account['approvals']:
                if account_approval['name'] == 'signup':
                    approval = account_approval
                    break
            
            if approval:
                if approval['state'] == 'PENDING':
                    # See above note. Actual production integrations should not
                    # approve blindly when receiving a message.
                    self.approve_account(account_id)
                
                elif approval['state'] == 'APPROVED':
                    # Now that it's approved, store a record in the database.
                    internal_id = _generate_internal_account_id()
                    customer = {
                        'procurement_account_id': account_id,
                        'internal_account_id': internal_id,
                        'products': {}
                    }
                    self.database.write(account_id, customer)
            else:
                # The account has been deleted, so delete the database record.
                if customer:
                    self.database.delete(account_id)
        
        # Always ack account messages. We only care about the above scenarios.
        return True
    
    ##############################
    ### Entitlement operations ###
    ##############################
    
    def _get_entitlement_name(self, entitlement_id):
        return f'providers/DEMO-{PROJECT_ID}/entitlements/{entitlement_id}'
    
    def get_entitlement(self, entitlement_id):
        """Gets an entitlement from the Procurement Service."""
        name = self._get_entitlement_name(entitlement_id)
        request = self.service.providers().entitlements().get(name=name)
        try:
            response = request.execute()
            return response
        except HttpError as err:
            if err.resp.status == 404:
                return None
    
    def approve_entitlement(self, entitlement_id):
        try:
            """Approves the entitlement in the Procurement Service."""
            name = self._get_entitlement_name(entitlement_id)
            
            request = self.service.providers().entitlements().approve(
                name=name, body={})
            request.execute()
            print('\n[ INFO ] Entitlement Approved\n')
        except Exception as e:
            print(f'\n[ WARNING ] Entitlement NOT approved. {e}\n')
    
    def approve_entitlement_plan_change(self, entitlement_id, new_pending_plan):
        try:
            """Approves the entitlement plan change in the Procurement Service."""
            name = self._get_entitlement_name(entitlement_id)
            body = {'pendingPlanName': new_pending_plan}
            
            request = self.service.providers().entitlements().approvePlanChange(
                name=name, body=body)
            request.execute()
            print('[ INFO ] Entitlement Plan change Approved')
        except Exception as e:
            print(f'\n[ WARNING ] Entitlement Plan change NOT approved. {e}\n')
    
    def handle_active_entitlement(self, entitlement, customer, account_id):
        print('\n[ INFO ] Updating the database to match the active entitlement.\n')
        
        product = {
            'product_id': entitlement['product'],
            'plan_id':    entitlement['plan'],
            'start_time': entitlement['createTime'],
        }
        
        if 'usageReportingId' in entitlement:
            product['consumer_id'] = entitlement['usageReportingId']
        
        customer['products'][entitlement['product']] = product
        
        ### TODO: Set up the service for the customer to use. ###
        print('\n[ INFO ] Setting up the service for the customer to use.\n')
        self.database.write(account_id, customer)
    
    def handle_entitlement_message(self, message, event_type):
        """Handles incoming Pub/Sub messages about entitlement resources."""
        entitlement_id = message['id']
        
        entitlement = self.get_entitlement(entitlement_id)
        
        print(f'\n[ INFO ] Entitlement: {json.dumps(entitlement,indent=4)}\n')
        
        if not entitlement:
            # Do nothing. The entitlement has to be canceled to be deleted, so
            # this has already been handled by a cancellation message.
            return True
        
        account_id = self._get_account_id(entitlement['account'])
        customer = self.database.read(account_id)
        
        state = entitlement['state']
        
        if not customer:
            # If the record for this customer does not exist, don't ack the
            # message and wait until an account message is handled and a record
            # is created.
            return False
        
        if event_type == 'ENTITLEMENT_CREATION_REQUESTED':
            if state == 'ENTITLEMENT_ACTIVATION_REQUESTED':
                # Approve the entitlement and wait for another message for when
                # it becomes active before setting up the service for the
                # customer and updating our records.
                self.approve_entitlement(entitlement_id)
                return True
        
        elif event_type == 'ENTITLEMENT_ACTIVE':
            if state == 'ENTITLEMENT_ACTIVE':
                # Handle an active entitlement by writing to the database.
                self.handle_active_entitlement(entitlement, customer, account_id)
                return True
        
        elif event_type == 'ENTITLEMENT_PLAN_CHANGE_REQUESTED':
            if state == 'ENTITLEMENT_PENDING_PLAN_CHANGE_APPROVAL':
                # Don't write anything to our database until the entitlement
                # becomes active within the Procurement Service.
                self.approve_entitlement_plan_change(
                    entitlement_id, entitlement['newPendingPlan'])
                return True
        
        elif event_type == 'ENTITLEMENT_PLAN_CHANGED':
            if state == 'ENTITLEMENT_ACTIVE':
                # Handle an active entitlement after a plan change.
                self.handle_active_entitlement(entitlement, customer, account_id)
                return True
        
        elif event_type == 'ENTITLEMENT_PLAN_CHANGE_CANCELLED':
            # Do nothing. We approved the original change, but we never recorded
            # it or changed the service level since it hadn't taken effect yet.
            print('\n[ INFO ] Entitle Plan Change Cancelled.\n')
            return True
        
        elif event_type == 'ENTITLEMENT_CANCELLED':
            if state == 'ENTITLEMENT_CANCELLED':
                print('\n[ INFO ] Canelling Entitlement Plan.')
                
                # Clear out our records of the customer's plan.
                if entitlement['product'] in customer['products']:
                    del customer['products'][entitlement['product']]
                    
                    print('[ INFO ] Updating User data in the user database/table.')
                    print("[ INFO ] Turning off the Customer's service.\n")
                    ### TODO: Update User data in user database/table.
                    ### TODO: Turn off customer's service.
                    self.database.write(account_id, customer)
                return True
        
        elif event_type == 'ENTITLEMENT_PENDING_CANCELLATION':
            # Do nothing. We want to cancel once it's truly canceled. For now
            # it's just set to not renew at the end of the billing cycle.
            return True
        
        elif event_type == 'ENTITLEMENT_CANCELLATION_REVERTED':
            # Do nothing. The service was already active, but now it's set to
            # renew automatically at the end of the billing cycle.
            return True
        
        elif event_type == 'ENTITLEMENT_DELETED':
            # Do nothing. The entitlement has to be canceled to be deleted, so
            # this has already been handled by a cancellation message.
            return True
        
        return False


def main():
    
    # Construct a service for the Partner Procurement API
    database    = JsonDatabase()
    procurement = Procurement(database)
    
    # Get the subscription object
    subscriber = pubsub_v1.SubscriberClient()
    subscription_path = subscriber.subscription_path(PROJECT_ID, PUBSUB_SUBSCRIPTION)
    
    def callback(message):
        try:
            """Callback for handling Cloud Pub/Sub messages."""
            payload = json.loads(message.data)
            
            pprint.pprint(f'\n{payload}\n')
            
            ack = False
            if 'entitlement' in payload:
                ack = procurement.handle_entitlement_message(payload['entitlement'], payload['eventType'])
            elif 'account' in payload:
                ack = procurement.handle_account_message(payload['account'])
            else:
                # If there's no account or entitlement, then just ack and ignore the
                # message. This should never happen.
                ack = True
            
            if ack:
                message.ack()
        except Exception as e:
            # Log and Handle Exception
            pass
    
    subscription = subscriber.subscribe(subscription_path, callback=callback)
    
    print(f'\nListening for messages on {subscription_path}\n')
    
    while True:
        try:
            subscription.result()
        except Exception as e_sub:
            #time.sleep(3)
            #print(f'Listening for messages on {subscription_path} threw an Exception: {e_sub}.')
            pass


if __name__ == '__main__':
    main()